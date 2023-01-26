import asyncio
import json
import os
import random
import re
import signal
import socket
import subprocess
import uuid
from datetime import datetime
from datetime import timedelta

import requests
from jupyter_client import KernelProvisionerBase
from jupyter_client.connect import LocalPortCache
from jupyter_client.launcher import launch_kernel as start_popen
from jupyter_client.utils import ensure_async
from jupyter_server.services.kernels.kernelmanager import AsyncMappingKernelManager
from traitlets import DottedObjectName


class SlurmAsyncMappingKernelManager(AsyncMappingKernelManager):
    use_pending_kernels = True

    async def _async_shutdown_kernel(self, kernel_id, now=False, restart=False):
        # If Kernel is up and running: use the default shutdown function.
        # If it's still pending: cancel start and remove it, once it's cancelled.
        km = self.get_kernel(kernel_id)
        if km and isinstance(km.provisioner, SlurmProvisioner) and not km.ready.done():
            if kernel_id in self._pending_kernels:
                fut = self._pending_kernels[kernel_id]
                fut.cancel()
            stopper = ensure_async(km.shutdown_kernel(now, False))
            fut = asyncio.ensure_future(
                self._remove_kernel_when_ready(kernel_id, stopper)
            )
            self._pending_kernels[kernel_id] = fut
        else:
            await super()._async_shutdown_kernel(kernel_id, now, restart)

    async def interrupt_kernel(self, kernel_id):
        # If Kernel is up and running: use the default interrupt function.
        # If it's still pending: cancel start and remove it, once it's cancelled.
        km = self.get_kernel(kernel_id)
        if km and isinstance(km.provisioner, SlurmProvisioner) and not km.ready.done():
            if kernel_id in self._pending_kernels:
                fut = self._pending_kernels[kernel_id]
                fut.cancel()
            stopper = ensure_async(km.shutdown_kernel(True, False))
            fut = asyncio.ensure_future(
                self._remove_kernel_when_ready(kernel_id, stopper)
            )
            self._pending_kernels[kernel_id] = fut
        else:
            await super().interrupt_kernel(kernel_id)

    shutdown_kernel = _async_shutdown_kernel


class SlurmProvisioner(KernelProvisionerBase):
    """
    :class:`SlurmProvisioner` is a concrete class of ABC
    :py:class:`KernelProvisionerBase` and is used when "slurm-provisioner" is
    specified in the kernel specification (``kernel.json``).  It provides
    functional parity to existing applications by launching the kernel via
    slurm and using :class:`subprocess.Popen` to manage its lifecycle.
    """

    slurm_allocation_id = None
    slurm_allocation_name = None
    slurm_allocation_nodelist = []
    slurm_allocation_node = None
    slurm_allocation_endtime = None
    slurm_jobstep_id = None

    alloc_storage_file = None

    state = ""
    kernel_config = {}
    ports_cached = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # This storage file is used by the frontend extension
        home = os.environ.get("HOME", "")
        path = f"{home}/.local/share/jupyter/runtime/slurm_provisioner.json"
        self.alloc_storage_file = path

        # Should never be the case. But the official provisioners check these ..
        if self.parent is None:
            raise Exception(
                "Could not start kernel. Check JupyterLab logs for more information."
            )

        # this is the configuration given in kernel.json. Configured via frontend extension
        self.kernel_config = self.parent.kernel_spec.metadata.get(
            "kernel_provisioner", {}
        ).get("config", {})

        # We need at least these configuration. For everything else we'll use default values.
        required_keys = {"kernel_argv", "project", "partition", "nodes", "runtime"}
        if not required_keys <= set(self.kernel_config.keys()):
            raise Exception(
                "Slurm Wrapper not configured correctly. Use the Slurm Wrapper sidebar extension to configure this kernel."
            )

        # Originally kernel_spec.argv is the unix process to start a kernel.
        # We will run these commands with `srun`
        self.parent.kernel_spec.argv = self.kernel_config["kernel_argv"]

        # kernel language can be changed. Depending on the kernel that will be started
        if self.kernel_config.get("kernel_language", "None") != "None":
            self.parent.kernel_spec.language = self.kernel_config["kernel_language"]

        # If we want to reuse an allocation, we have to know its id
        self.slurm_allocation_id = self.kernel_config.get("jobid", None)

    # The following storage / update function are used store specific kernel information
    # that will be used by the frontend extension

    def read_local_storage_file(self):
        try:
            with open(self.alloc_storage_file, "r") as f:
                alloc_dict = json.load(f)
        except:
            alloc_dict = {}
        return alloc_dict

    def write_local_storage_file(self, data):
        try:
            with open(self.alloc_storage_file, "w") as f:
                f.write(json.dumps(data, indent=2, sort_keys=True))
        except:
            os.makedirs(os.path.dirname(self.alloc_storage_file), exist_ok=True)
            with open(self.alloc_storage_file, "w") as f:
                f.write(json.dumps(data, indent=2, sort_keys=True))

    def update_alloc_storage_status(self, status):
        alloc_storage = self.read_local_storage_file()
        alloc_storage[self.slurm_allocation_id]["state"] = status
        self.write_local_storage_file(alloc_storage)

    def update_alloc_storage_nodelist_endtime(self):
        self.slurm_allocation_endtime = (
            datetime.now() + timedelta(minutes=int(self.kernel_config["runtime"]))
        ).timestamp()
        alloc_storage = self.read_local_storage_file()
        alloc_storage[self.slurm_allocation_id][
            "nodelist"
        ] = self.slurm_allocation_nodelist
        alloc_storage[self.slurm_allocation_id][
            "endtime"
        ] = self.slurm_allocation_endtime
        self.write_local_storage_file(alloc_storage)

    def update_alloc_storage_add_kernel_id(self):
        alloc_storage = self.read_local_storage_file()
        if self.kernel_id not in alloc_storage[self.slurm_allocation_id]["kernel_ids"]:
            alloc_storage[self.slurm_allocation_id]["kernel_ids"].append(self.kernel_id)
        self.write_local_storage_file(alloc_storage)

    def update_alloc_storage_del_kernel_id(self):
        alloc_storage = self.read_local_storage_file()
        if self.kernel_id in alloc_storage[self.slurm_allocation_id]["kernel_ids"]:
            alloc_storage[self.slurm_allocation_id]["kernel_ids"].remove(self.kernel_id)
        self.write_local_storage_file(alloc_storage)

    def update_alloc_storage_del_allocation(self, alloc_id):
        alloc_storage = self.read_local_storage_file()
        if alloc_id in alloc_storage.keys():
            del alloc_storage[alloc_id]
        self.write_local_storage_file(alloc_storage)

    def update_alloc_storage_init_allocation(self):
        # Ensure allocation is part of alloc_storage file
        alloc_storage = self.read_local_storage_file()
        if self.slurm_allocation_id not in alloc_storage.keys():
            alloc_storage[self.slurm_allocation_id] = {
                "kernel_ids": [],
                "nodelist": [],
                "endtime": None,
                "config": self.kernel_config,
                "state": "PENDING",
            }
        self.write_local_storage_file(alloc_storage)

    @property
    def has_process(self):
        """
        Returns true if this provisioner is currently managing a process.

        This property is asserted to be True immediately following a call to
        the provisioner's :meth:`launch_kernel` method.
        """
        return self.state and self.state not in ["FINISHED", "FAILED"]

    async def poll(self):
        """
        Checks if kernel process is still running.

        If running, None is returned, otherwise the process's integer-valued exit code is returned.
        This method is called from :meth:`KernelManager.is_alive`.
        """
        if self.state in ["FINISHED", "FAILED"]:
            return 1
        else:
            if (
                self.slurm_allocation_endtime
                and datetime.now().timestamp() > self.slurm_allocation_endtime
            ):
                # Allocation ran out of time. Call self.kill() to update storage file correctly
                self.log.debug(
                    "Allocation {self.slurm_allocation_id} ran out of time. Update storage files. Return 1"
                )
                await self.kill()
                return 1
            else:
                return None

    async def wait(self, grace_period_sec=10):
        """
        Waits for kernel process to terminate.

        This method is called from `KernelManager.finish_shutdown()` and
        `KernelManager.kill_kernel()` when terminating a kernel gracefully or
        immediately, respectively.
        """
        c = 0
        for i in range(grace_period_sec):
            if self.state in ["FINISHED", "FAILED"]:
                return
            await asyncio.sleep(1)

    async def send_signal(self, signum):
        """
        Sends signal identified by signum to the kernel process.

        This method is called from `KernelManager.signal_kernel()` to send the
        kernel process a signal.
        """
        if signum == 0:
            return await self.poll()
        elif signum in [signal.SIGKILL, signal.SIGINT]:
            return await self.kill()
        return

    async def slurm_stop_jobstep(self):
        # Kill kernel, but do not stop allocation
        # Remove kernel_id from storage file
        scancel_cmd = ["scancel", self.slurm_jobstep_id]
        try:
            subprocess.check_output(scancel_cmd)
        except:
            self.log.exception("Could not stop jobstep")
        finally:
            self.update_alloc_storage_del_kernel_id()

        self.log.info("Wait for scancel to finish")
        cancel_time = (datetime.now() + timedelta(seconds=120)).timestamp()
        while datetime.now().timestamp() < cancel_time:
            # Wait until jobstep is no longer listed in sacct, but max 120 seconds
            sacct_cmd = [
                "sacct",
                "-o",
                "jobid",
                "-n",
                "-P",
                "-s",
                "RUNNING",
                "--name",
                str(self.slurm_allocation_name),
            ]
            all_steps_list = (
                subprocess.check_output(sacct_cmd).decode().strip().split("\n")
            )
            self.log.info(f"Wait for scancel to finish - {all_steps_list}")
            if self.slurm_jobstep_id in all_steps_list:
                await asyncio.sleep(1)
            else:
                return

    async def slurm_stop_allocation(self, alloc_id):
        # Check if something's still running on this allocation
        # Do not use self.slurm_allocation_id - this might be deleted
        # within the next 10 seconds.
        await asyncio.sleep(10)
        sacct_cmd = [
            "sacct",
            "-o",
            "jobid",
            "-n",
            "--delimiter",
            ";",
            "-P",
            "-s",
            "RUNNING,PENDING",
        ]
        all_jobs_list = subprocess.check_output(sacct_cmd).decode().strip().split("\n")
        all_jobs_in_alloc = [x for x in all_jobs_list if x.startswith(alloc_id)]
        if len(all_jobs_in_alloc) == 0:
            # allocation alloc_id is not running/pending
            pass
        elif len(all_jobs_in_alloc) > 1:
            # There are still job_steps running on this alloc_id.
            # Do not cancel this allocation
            pass
        else:
            # No Jobstep running on this allocation - kill it
            scancel_cmd = ["scancel", alloc_id]
            subprocess.check_output(scancel_cmd)
            self.update_alloc_storage_del_allocation(alloc_id)

    async def kill(self, restart=False):
        """
        Kill the kernel process.

        This is typically accomplished via a SIGKILL signal, which cannot be caught.
        This method is called from `KernelManager.kill_kernel()` when terminating
        a kernel immediately.

        restart is True if this operation will precede a subsequent launch_kernel request.
        """
        # even if restart is true, we're still checking for running kernels on allocation
        # after 10 seconds. If a kernel restart would fail, the allocation might be running
        # for a long time.

        try:
            if self.slurm_salloc_process:
                # If it's currently allocating slurm resources: kill this process
                self.slurm_salloc_process.kill()
        except:
            self.log.exception("Could not kill allocation process")
        try:
            if self.slurm_launch_kernel_process:
                # If it's currently allocating slurm resources: kill this process
                self.slurm_launch_kernel_process.kill()
                # Make sure all the fds get closed.
                for attr in ["stdout", "stderr", "stdin"]:
                    fid = getattr(self.slurm_launch_kernel_process, attr)
                    if fid:
                        fid.close()
                self.slurm_launch_kernel_process = None
        except:
            self.log.exception("Could not kill launch kernel process")

        try:
            if self.slurm_jobstep_id:
                # scancel command if jobstep id exists
                await self.slurm_stop_jobstep()
        except:
            self.log.exception("Could not stop jobstep")
        finally:
            try:
                if self.slurm_allocation_id:
                    # scancel command if allocation id exists ; and no jobsteps are running on it
                    asyncio.create_task(
                        self.slurm_stop_allocation(self.slurm_allocation_id)
                    )
            except:
                self.log.exception("Could not stop allocation")
            finally:
                self.state = "FINISHED"

    async def terminate(self, restart=False):
        """
        Terminates the kernel process.

        This is typically accomplished via a SIGTERM signal, which can be caught, allowing
        the kernel provisioner to perform possible cleanup of resources.  This method is
        called indirectly from `KernelManager.finish_shutdown()` during a kernel's
        graceful termination.

        restart is True if this operation precedes a start launch_kernel request.
        """
        await self.kill(restart)

    async def pre_launch(self, **kwargs):
        # ensure that an allocation is up and running
        self.state = "PRE_LAUNCH"
        ret = await super().pre_launch(**kwargs)
        ret["cmd"] = None
        return ret

    async def slurm_verify_allocation(self):
        # If an slurm_allocation_id is given via config,
        # we verify that it's running (or pending) and
        # store the jobname
        if not self.slurm_allocation_name:
            sacct_cmd = [
                "sacct",
                "-o",
                "jobid,jobname",
                "-n",
                "--delimiter",
                ";",
                "-P",
                "-s",
                "RUNNING,PENDING",
            ]
            all_jobs_raw = subprocess.check_output(sacct_cmd).decode().strip()
            all_jobs_tuples = [x.split(";") for x in all_jobs_raw.split("\n")]
            slurm_jobs_name_list = [
                x[1] for x in all_jobs_tuples if x[0] == self.slurm_allocation_id
            ]
            self.slurm_allocation_name = (
                slurm_jobs_name_list[0] if slurm_jobs_name_list else None
            )
            if not self.slurm_allocation_name:
                raise Exception(
                    f"Allocation {self.slurm_allocation_id} is not running. Shutdown or interrupt this kernel and start a new kernel afterwards. Use the SlurmWrapper sidebar extension to configure it properly."
                )

        # Ensure it's stored in storage file
        self.update_alloc_storage_init_allocation()

        if (not self.slurm_allocation_nodelist) or (not self.slurm_allocation_endtime):
            storage_file = self.read_local_storage_file()
            self.slurm_allocation_nodelist = storage_file.get(
                self.slurm_allocation_id, {}
            ).get("nodelist", [])
            self.slurm_allocation_endtime = storage_file.get(
                self.slurm_allocation_id, {}
            ).get("endtime", 0)
            await self.slurm_allocation_store_nodelist()

    async def slurm_allocate(self):
        # start slurm allocation
        # Do not run anything on it yet
        get_budget_account_cmd = [
            "/usr/libexec/jutil-exe",
            "env",
            "activate",
            "-p",
            self.kernel_config["project"],
        ]
        try:
            get_budget_account_output = (
                subprocess.check_output(get_budget_account_cmd).decode().strip()
            )
        except Exception as e:
            raise e
        jutil_vars = {
            x.group(1): x.group(2)
            for x in re.finditer(r"export\ ([^=]+)=([^;]+)", get_budget_account_output)
        }

        self.slurm_allocation_name = uuid.uuid4().hex
        salloc_cmd = [
            "salloc",
            "--no-shell",
            "--account",
            str(jutil_vars["BUDGET_ACCOUNTS"]),
            "--partition",
            str(self.kernel_config["partition"]),
            "--nodes",
            str(self.kernel_config["nodes"]),
            "--time",
            str(self.kernel_config["runtime"]),
            "--job-name",
            str(self.slurm_allocation_name),
            "--begin",
            "now",
        ]
        if self.kernel_config.get("gpus", "0") not in ["0", "", None]:
            salloc_cmd += [f"--gres=gpu:{self.kernel_config['gpus']}"]
        if self.kernel_config.get("reservation", "None") != "None":
            salloc_cmd += ["--reservation", str(self.kernel_config["reservation"])]

        self.slurm_salloc_process = start_popen(salloc_cmd)
        exit_code = self.slurm_salloc_process.wait()
        # Make sure all the fds get closed.
        for attr in ["stdout", "stderr", "stdin"]:
            fid = getattr(self.slurm_salloc_process, attr)
            if fid:
                fid.close()
        self.slurm_salloc_process = None
        if exit_code != 0:
            raise Exception(
                "Could not create slurm allocation. Check JupyterLab logs for more information."
            )

        # Get JobID for defined job-name
        squeue_cmd = [
            "squeue",
            "-o",
            "%i",
            "-h",
            "--name",
            str(self.slurm_allocation_name),
        ]
        self.slurm_allocation_id = subprocess.check_output(squeue_cmd).decode().strip()

        # Ensure it's stored in storage file
        self.update_alloc_storage_init_allocation()

    async def slurm_allocation_running(self):
        # Wait for slurm allocation until it's running
        squeue_cmd = [
            "squeue",
            "-o",
            "%T",
            "-h",
            "--name",
            str(self.slurm_allocation_name),
        ]
        unknown_allocation = False
        prev_state = ""
        while True:
            slurm_allocation_status = (
                subprocess.check_output(squeue_cmd).decode().strip()
            )
            if not slurm_allocation_status:
                if unknown_allocation:
                    # second time no allocation; let's cancel this
                    raise Exception(
                        f"Allocation {self.slurm_allocation_id} is not running. Shutdown or interrupt this kernel and start a new kernel afterwards. Use the SlurmWrapper sidebar extension to configure it properly."
                    )
                else:
                    # No state means no allocation with this name available. Wait for 10 seconds and try again
                    unknown_allocation = True
                    await asyncio.sleep(10)
                    continue

            if prev_state != slurm_allocation_status:
                self.update_alloc_storage_status(slurm_allocation_status)
                prev_state = slurm_allocation_status

            unknown_allocation = False
            if slurm_allocation_status == "RUNNING":
                # If it's up and running, we can go on and start a kernel on this allocation
                break
            elif slurm_allocation_status in ["CANCELLED", "FAILED"]:
                # That's not good. Abort kernel launch
                raise Exception(
                    f"Allocation {self.slurm_allocation_id} is not running. Shutdown or interrupt this kernel and start a new kernel afterwards. Use the SlurmWrapper sidebar extension to configure it properly."
                )
            else:
                # Check again in 5 seconds
                await asyncio.sleep(5)

    async def slurm_allocation_store_nodelist(self):
        sacct_cmd = [
            "sacct",
            "-o",
            "nodelist",
            "-n",
            "-P",
            "-s",
            "RUNNING,PENDING",
            "--name",
            str(self.slurm_allocation_name),
        ]
        nodelist_raw = (
            subprocess.check_output(sacct_cmd).decode().strip().split("\n")[0]
        )
        # sacct shows nodelist like this:
        # jsfc078
        # jsfc[078-079]
        # jsfc[018-029,031-049,052]
        # make a usable python list out of this
        if "[" not in nodelist_raw:
            self.slurm_allocation_nodelist = [nodelist_raw]
        else:
            self.slurm_allocation_nodelist = []
            prefix, all_numbers = nodelist_raw.split("[")
            all_numbers = all_numbers.rstrip("]")
            all_numbers_blocks = all_numbers.split(",")
            for block in all_numbers_blocks:
                block_s = block.split("-")
                start_s = block_s[0]
                if len(block_s) == 2:
                    end_s = block_s[1]
                else:
                    end_s = start_s
                start = int(start_s)
                end = int(end_s)
                len_s = len(start_s)
                for i in range(start, end + 1):
                    self.slurm_allocation_nodelist.append(
                        f"{prefix}{str(i).zfill(len_s)}"
                    )

        self.update_alloc_storage_nodelist_endtime()

    async def slurm_launch_kernel(self, **kwargs):
        km = self.parent
        extra_arguments = kwargs.pop("extra_arguments", [])
        kernel_cmd = km.format_kernel_cmd(extra_arguments=extra_arguments)
        self.slurm_allocation_node = random.choice(self.slurm_allocation_nodelist)
        srun_cmd = [
            "srun",
            "--jobid",
            self.slurm_allocation_id,
            "-w",
            self.slurm_allocation_node,
            "-N",
            "1",
            "--ntasks",
            "1",
            "--job-name",
            self.kernel_id,
            "--exclusive",
        ] + kernel_cmd
        self.slurm_launch_kernel_process = start_popen(srun_cmd)
        self.update_alloc_storage_add_kernel_id()

    async def set_connection_info(self):
        # Starting a kernel on a compute node, nearly all ports are free.
        # If there's something else running on this compute node, we cannot
        # check it at this stage. But we can ensure, that multiple kernels
        # on one allocation do not impede each other
        lpc = LocalPortCache.instance()
        km = self.parent
        if km.cache_ports and not self.ports_cached:
            km.ip = "127.0.0.1"
            km.shell_port = lpc.find_available_port(km.ip)
            km.iopub_port = lpc.find_available_port(km.ip)
            km.stdin_port = lpc.find_available_port(km.ip)
            km.hb_port = lpc.find_available_port(km.ip)
            km.control_port = lpc.find_available_port(km.ip)
            self.ports_cached = True
            # For some networks you have to use a suffix for internal communication
            suffix = os.environ.get("SLURM_PROVISIONER_NODE_SUFFIX", "")
            km.ip = socket.gethostbyname(f"{self.slurm_allocation_node}{suffix}")
        km.write_connection_file()
        self.connection_info = km.get_connection_info()

    async def launch_kernel(self, cmd, **kwargs):
        """
        Launch the kernel process and return its connection information.

        This method is called from `KernelManager.launch_kernel()` during the
        kernel manager's start kernel sequence.
        """
        try:
            if (
                not self.slurm_allocation_id
            ) or self.slurm_allocation_id.lower() == "none":
                await self.slurm_allocate()
            else:
                await self.slurm_verify_allocation()
            await self.slurm_allocation_running()
            await self.slurm_allocation_store_nodelist()
        except Exception as e:
            self.log.exception("Exception in pre_launch")
            self.state = "FAILED"
            raise Exception(str(e))

        self.state = "LAUNCH"
        try:
            await self.slurm_launch_kernel()
            await self.set_connection_info()
        except Exception as e:
            self.log.exception("Excpetion in launch_kernel.")
            self.state = "FAILED"
            raise Exception(str(e))

        return self.connection_info

    async def slurm_set_jobstep_id(self):
        sacct_cmd = [
            "sacct",
            "-o",
            "jobid,jobname",
            "-n",
            "--delimiter",
            ";",
            "-P",
            "-s",
            "RUNNING",
            "--name",
            str(self.slurm_allocation_name),
        ]
        self.log.info("Wait for jobstep to be listed")
        cancel_time = (datetime.now() + timedelta(seconds=120)).timestamp()
        while datetime.now().timestamp() < cancel_time:
            # Wait until jobstep is no longer listed in sacct, but max 120 seconds
            all_steps_raw = subprocess.check_output(sacct_cmd).decode().strip()
            all_steps_tuples = [x.split(";") for x in all_steps_raw.split("\n")]
            slurm_jobstep_id_list = [
                x[0] for x in all_steps_tuples if x[1] == self.kernel_id
            ]
            self.log.info(f"Wait for jobstep to be listed - {all_steps_tuples}")
            if not slurm_jobstep_id_list:
                await asyncio.sleep(1)
            else:
                self.slurm_jobstep_id = (
                    slurm_jobstep_id_list[-1] if slurm_jobstep_id_list else None
                )
                return
        raise Exception("Could not receive slurm jobid for kernel")

    async def send_metric_to_jhub(self):
        jhub_metrics_url = os.environ.get("SLURM_PROVISIONER_JHUB_METRICS", "")
        jhub_token = os.environ.get("JUPYTERHUB_API_TOKEN", "")
        if jhub_metrics_url and jhub_token:
            body = {
                "kernel_id": self.kernel_id,
                "slurm_allocation_id": self.slurm_allocation_id,
                "slurm_allocation_name": self.slurm_allocation_name,
                "slurm_allocation_nodelist": self.slurm_allocation_nodelist,
                "slurm_allocation_node": self.slurm_allocation_node,
                "slurm_allocation_endtime": self.slurm_allocation_endtime,
                "slurm_jobstep_id": self.slurm_jobstep_id,
                "config": self.kernel_config,
            }
            headers = {"Authorization": f"token {jhub_token}"}
            self.log.info("Send metrics to JupyterHub", extra=body)
            ca_path_env = os.environ.get("JUPYTERHUB_CERTIFICATE", False)
            ca_path = ca_path_env if ca_path_env else False
            with requests.post(
                jhub_metrics_url, json=body, headers=headers, verify=ca_path
            ) as r:
                r.raise_for_status()

    async def post_launch(self, **kwargs):
        self.state = "POST_LAUNCH"
        try:
            await self.slurm_set_jobstep_id()
        except Exception as e:
            self.log.exception("Excpetion in post_launch.")
            self.state = "FAILED"
            raise Exception(str(e))
        try:
            await self.send_metric_to_jhub()
        except:
            self.log.warning(
                "Could not send metric to JupyterHub. Start Kernel anyway",
                exc_info=True,
            )

        self.state = "RUNNING"

    async def cleanup(self, restart=False):
        """
        Cleanup any resources allocated on behalf of the kernel provisioner.

        This method is called from `KernelManager.cleanup_resources()` as part of
        its shutdown kernel sequence.

        restart is True if this operation precedes a start launch_kernel request.
        """
        lpc = LocalPortCache.instance()
        if self.ports_cached and "shell_port" in self.connection_info and not restart:
            ports = (
                self.connection_info["shell_port"],
                self.connection_info["iopub_port"],
                self.connection_info["stdin_port"],
                self.connection_info["hb_port"],
                self.connection_info["control_port"],
            )
            for port in ports:
                lpc.return_port(port)

    async def get_provisioner_info(self):
        """Captures the base information necessary for persistence relative to this instance."""
        provisioner_info = await super().get_provisioner_info()
        provisioner_info.update(
            {
                "slurm_allocation_id": self.slurm_allocation_id,
                "slurm_allocation_name": self.slurm_allocation_name,
                "slurm_allocation_nodelist": self.slurm_allocation_nodelist,
                "slurm_allocation_node": self.slurm_allocation_node,
                "slurm_allocation_endtime": self.slurm_allocation_endtime,
                "slurm_jobstep_id": self.slurm_jobstep_id,
            }
        )
        return provisioner_info

    async def load_provisioner_info(self, provisioner_info):
        """Loads the base information necessary for persistence relative to this instance."""
        await super().load_provisioner_info(provisioner_info)
        self.slurm_allocation_id = provisioner_info["slurm_allocation_id"]
        self.slurm_allocation_name = provisioner_info["slurm_allocation_name"]
        self.slurm_allocation_nodelist = provisioner_info["slurm_allocation_nodelist"]
        self.slurm_allocation_node = provisioner_info["slurm_allocation_node"]
        self.slurm_allocation_endtime = provisioner_info["slurm_allocation_endtime"]
        self.slurm_jobstep_id = provisioner_info["slurm_jobstep_id"]
