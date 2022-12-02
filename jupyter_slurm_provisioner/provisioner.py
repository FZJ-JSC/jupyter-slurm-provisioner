import asyncio
import json
import os
import random
import signal
import socket
import subprocess
import uuid
from datetime import datetime
from datetime import timedelta
from typing import Any
from typing import Dict
from typing import List
from typing import Optional
from typing import Tuple
from tornado.web import HTTPError

from jupyter_client import KernelProvisionerBase
from jupyter_client.connect import KernelConnectionInfo
from jupyter_client.connect import LocalPortCache
from jupyter_client.launcher import launch_kernel


class SlurmProvisioner(KernelProvisionerBase):
    """
    :class:`SlurmProvisioner` is a concrete class of ABC
    :py:class:`KernelProvisionerBase` and is used when "slurm-provisioner" is
    specified in the kernel specification (``kernel.json``).  It provides
    functional parity to existing applications by launching the kernel via
    slurm and using :class:`subprocess.Popen` with bash scripts to manage its
    lifecycle.
    """

    alloc_id = None
    alloc_listnode = []
    alloc_storage_file = ""
    node = ""

    process = None
    pid = None
    ports_cached = False

    @property
    def has_process(self) -> bool:
        return self.process is not None

    async def poll(self) -> Optional[int]:
        ret = 0
        if self.process:
            ret = self.process.poll()
        return ret

    async def wait(self, set_process_none=True) -> Optional[int]:
        ret = 0
        if self.process:
            # Use busy loop at 100ms intervals, polling until the process is
            # not alive.  If we find the process is no longer alive, complete
            # its cleanup via the blocking wait().  Callers are responsible for
            # issuing calls to wait() using a timeout (see kill()).
            while await self.poll() is None:
                await asyncio.sleep(0.1)

            # Process is no longer alive, wait and clear
            ret = self.process.wait()
            # Make sure all the fds get closed.
            for attr in ["stdout", "stderr", "stdin"]:
                fid = getattr(self.process, attr)
                if fid:
                    fid.close()
            if set_process_none:
                self.process = None  # allow has_process to now return False
        return ret

    def nodeListToListNode(self, nodelist_str) -> List:
        # sacct shows nodelist like this:
        # jsfc078
        # jsfc[078-079]
        # jsfc[018-029,031-049,052]
        # make a usable python list out of this
        if "[" not in nodelist_str:
            return [nodelist_str]

        ret = []
        prefix, all = nodelist_str.split("[")
        all = all.rstrip("]")
        blocks = all.split(",")
        for block in blocks:
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
                ret.append(f"{prefix}{str(i).zfill(len_s)}")
        return ret

    def read_local_storage_file(self) -> Dict:
        if not self.alloc_storage_file:
            home = os.environ.get("HOME", "")
            path = f"{home}/.local/share/jupyter/runtime/slurm_provisioner.json"
            self.alloc_storage_file = path
        try:
            with open(self.alloc_storage_file, "r") as f:
                alloc_dict = json.load(f)
        except:
            alloc_dict = {}
        return alloc_dict

    def write_local_storage_file(self, data) -> None:
        if not self.alloc_storage_file:
            home = os.environ.get("HOME", "")
            path = f"{home}/.local/share/jupyter/runtime/slurm_provisioner.json"
            self.alloc_storage_file = path
        with open(self.alloc_storage_file, "w") as f:
            f.write(json.dumps(data, indent=2, sort_keys=True))

    async def kill_allocation(self, alloc_id):
        await asyncio.sleep(10)
        alloc_dict = self.read_local_storage_file()
        if len(alloc_dict.get(alloc_id, {}).get("kernel_ids", [])) == 0:
            self.log.info(f"Stop Slurmel Allocation {alloc_id}")
            scancel_alloc_cmd = ["slurmel_cancel", str(alloc_id)]
            try:
                subprocess.check_output(scancel_alloc_cmd)
            except:
                raise HTTPError(400, f"Could not cancel slurm allocation ( {alloc_id} ).")
            finally:
                alloc_dict = self.read_local_storage_file()
                if alloc_id in alloc_dict.keys():
                    del alloc_dict[alloc_id]
                    self.write_local_storage_file(alloc_dict)

    async def cancel(self) -> None:
        # Remove KernelID local user storage file
        alloc_dict = self.read_local_storage_file()
        if self.kernel_id in alloc_dict.get(self.alloc_id, {}).get("kernel_ids", []):
            alloc_dict[self.alloc_id]["kernel_ids"].remove(self.kernel_id)
        self.write_local_storage_file(alloc_dict)

        self.log.info(f"Stop Slurmel Kernel {self.kernel_id}")
        scancel_kernel_cmd = ["slurmel_cancel", str(self.kernel_id)]
        try:
            subprocess.check_output(scancel_kernel_cmd)
        except:
            raise HTTPError(400, f"Could not cancel slurm jobstep on allocation {self.alloc_id}.")
        finally:
            if len(alloc_dict.get(self.alloc_id, {}).get("kernel_ids", [])) == 0:
                # No kernels left on alloc - kill allocation in extra task
                # if there's another kernel for this allocation in 30 seconds it
                # was probably just a restart and we want to reuse the allocation
                asyncio.create_task(self.kill_allocation(self.alloc_id))

    async def send_signal(self, signum: int) -> None:
        if signum == signal.SIGINT or signum == signal.SIGKILL:
            await self.cancel()

    async def kill(self, restart: bool = False) -> None:
        await self.cancel()

    async def terminate(self, restart: bool = False) -> None:
        await self.cancel()

    @staticmethod
    def _tolerate_no_process(os_error: OSError) -> None:
        # On Unix, we may get an ESRCH error (or ProcessLookupError instance) if
        # the process has already terminated. Ignore it.
        from errno import ESRCH

        if not isinstance(os_error, ProcessLookupError) or os_error.errno != ESRCH:
            raise

    async def cleanup(self, restart: bool = False) -> None:
        if self.ports_cached and not restart:
            # provisioner is about to be destroyed, return cached ports
            lpc = LocalPortCache.instance()
            ports = (
                self.connection_info["shell_port"],
                self.connection_info["iopub_port"],
                self.connection_info["stdin_port"],
                self.connection_info["hb_port"],
                self.connection_info["control_port"],
            )
            for port in ports:
                lpc.return_port(port)

    async def get_job_id(self, unique_identifier, retries=5) -> Tuple[str]:
        sacct_cmd = ["slurmel_allocinfo", unique_identifier]
        job_info = ""
        c = 0
        while not job_info and c < retries:
            job_info = subprocess.check_output(sacct_cmd).decode()
            await asyncio.sleep(0.5)
            c += 1

        if not job_info:
            raise Exception("Could not receive Job ID for salloc cmd")

        return job_info.split(";;;")[0], self.nodeListToListNode(
            job_info.split(";;;")[1]
        )

    async def add_allocation_to_kernel_json_file(self):
        home = os.environ.get("HOME", "")
        path = f"{home}/.local/share/jupyter/kernels/slurm-provisioner-kernel/kernel.json"
        with open(path, "r") as f:
            kernel_json = json.load(f)
        if "config" in kernel_json.get("metadata", {}).get("kernel_provisioner", {}).keys():
            kernel_json["metadata"]["kernel_provisioner"]["config"]["jobid"] = self.alloc_id
        with open(path, "w") as f:
            f.write(json.dumps(kernel_json, indent=4, sort_keys=True))

    async def allocate_slurm_job(self, km, kernel_config, **kwargs) -> None:
        unique_identifier = uuid.uuid4().hex
        salloc_cmd = [
            "slurmel_allocate",
            "-a",
            str(kernel_config["project"]),
            "-p",
            str(kernel_config["partition"]),
            "-n",
            str(kernel_config["nodes"]),
            "-t",
            str(kernel_config["runtime"]),
            "-i",
            str(unique_identifier),
        ]
        if kernel_config.get("gpus", "0") != "0":
            salloc_cmd += ["-g", kernel_config["gpus"]]
        if kernel_config.get("reservation", "None") != "None":
            salloc_cmd += ["-r", kernel_config["reservation"]]
        
        # Start allocation, do not wait for it
        subprocess.check_output(salloc_cmd)

        # Check for jobid, nodelist will be none
        self.alloc_id, _ = await self.get_job_id(unique_identifier, retries=40)
        
        # Add Allocation ID to kernel.json file. This way it's reused for the next kernel
        await self.add_allocation_to_kernel_json_file()
        
        # Add Slurm-JobID with empty nodelist to local user storage file
        alloc_dict = self.read_local_storage_file()
        alloc_dict[self.alloc_id] = {
            "kernel_ids": [self.kernel_id],
            "nodelist": [],
            "endtime": None,
            "config": kernel_config,
            "state": "PENDING"
        }
        self.write_local_storage_file(alloc_dict)

        # Now we will wait here until the job is running. Then we know the nodelist etc.
        salloc_wait_cmd = [
            "slurmel_allocwait",
            "-i",
            str(unique_identifier)
        ]
        self.process = launch_kernel(salloc_wait_cmd, **kwargs)

        # Wait until job is running, so we can check for the node list
        self.pid = self.process.pid
        ret = await self.wait(set_process_none=False)
        if ret != 0:
            raise HTTPError(400, "Could not allocate slurm allocation. Check JupyterLab logs for more information.")

        # Allocation started succesful, let's get jobid
        self.alloc_id, self.alloc_listnode = await self.get_job_id(unique_identifier)

        # Add Slurm-JobID with it's nodelist to local user storage file
        alloc_dict = self.read_local_storage_file()
        alloc_dict[self.alloc_id]["nodelist"] = self.alloc_listnode
        alloc_dict[self.alloc_id]["endtime"] = ( datetime.now() + timedelta(minutes=int(kernel_config["runtime"])) ).timestamp()
        alloc_dict[self.alloc_id]["pid"] = None
        self.write_local_storage_file(alloc_dict)

    async def pre_launch(self, **kwargs: Any) -> Dict[str, Any]:
        """Perform any steps in preparation for kernel process launch.

        This includes applying additional substitutions to the kernel launch command and env.
        It also includes preparation of launch parameters.

        Returns the updated kwargs.
        """

        # This should be considered temporary until a better division of labor can be defined.
        km = self.parent
        if km is None:
            raise HTTPError(status_code=400, log_message="Could not load kernel. You should restart JupyterLab and try again.")
        self.alloc_storage_file = (
            f"{os.path.dirname(km.connection_file)}/slurm_provisioner.json"
        )
        kernel_config = km.kernel_spec.metadata.get("kernel_provisioner", {}).get(
            "config", {}
        )
        required_keys = {"kernel_argv", "project", "partition", "nodes", "runtime"}
        if not required_keys <= set(kernel_config.keys()):
            error_msg = "Slurm Wrapper not configured correctly. Use the SlurmWrapper sidebar extension to configure this kernel."
            raise HTTPError(status_code=400, log_message=error_msg)

        km.kernel_spec.argv = kernel_config["kernel_argv"]

        if kernel_config.get("kernel_language", "None") != "None":
            km.kernel_spec.language = kernel_config["kernel_language"]

        allocate_job = True
        if kernel_config.get("jobid", "None") != "None":
            # use preexisting jobid, do not allocate new slurm job
            job_id_config = str(kernel_config.get("jobid"))
            sacct_cmd = ["slurmel_jobinfo", job_id_config]
            job_id = subprocess.check_output(sacct_cmd).decode()
            if job_id:
                try:
                    allocate_job = False
                    self.alloc_id = job_id
                    alloc_dict = self.read_local_storage_file()
                    self.alloc_listnode = alloc_dict[self.alloc_id]["nodelist"]
                except:
                    raise HTTPError(400, "Could not restart kernel. Check JupyterLab logs for more information.")
            else:
                raise HTTPError(400, f"Could not restart kernel. Allocation {job_id_config} is no longer running. Use the SlurmWrapper sidebar extension to configure this kernel.")
        if allocate_job:
            await self.allocate_slurm_job(km, kernel_config, **kwargs)

        kernel_config["jobid"] = self.alloc_id

        if kernel_config.get("node", "None") != "None":
            self.node = kernel_config["node"]
        else:
            self.node = random.choice(self.alloc_listnode)
        if self.node not in self.alloc_listnode:
            self.log.warning(
                f"Unsupported node selected {self.node} / {self.alloc_listnode}"
            )
            self.log.warning("Use random node of listnode")
            self.node = random.choice(self.alloc_listnode)

        kernel_config["node"] = self.node

        # build the Popen cmd
        extra_arguments = kwargs.pop("extra_arguments", [])

        # write connection file / get default ports
        if km.cache_ports and not self.ports_cached:
            lpc = LocalPortCache.instance()
            km.ip = "127.0.0.1"
            km.shell_port = lpc.find_available_port(km.ip)
            km.iopub_port = lpc.find_available_port(km.ip)
            km.stdin_port = lpc.find_available_port(km.ip)
            km.hb_port = lpc.find_available_port(km.ip)
            km.control_port = lpc.find_available_port(km.ip)
            self.ports_cached = True
            # In JSC we need the `i` suffix for internal communication
            suffix = os.environ.get("SLURM_PROVISIONER_NODE_SUFFIX", "")
            km.ip = socket.gethostbyname(f"{self.node}{suffix}")

        km.write_connection_file()
        self.connection_info = km.get_connection_info()

        kernel_cmd = km.format_kernel_cmd(
            extra_arguments=extra_arguments
        )  # This needs to remain here for b/c
        kernel_cmd = [
            "slurmel_kernel_start",
            self.alloc_id,
            self.node,
            km.kernel_id,
        ] + kernel_cmd
        # self.log.info(" ".join(kernel_cmd))
        try:
            ret = await super().pre_launch(cmd=kernel_cmd, **kwargs)
        except:
            raise HTTPError(400, "Could not allocate slurm job. The JupyterLab log contains more information.")
        return ret

    async def launch_kernel(
        self, cmd: List[str], **kwargs: Any
    ) -> KernelConnectionInfo:
        # cmd is kernel.json.argv - kwargs is cwd and env
        scrubbed_kwargs = SlurmProvisioner._scrub_kwargs(kwargs)
        try:
            self.process = launch_kernel(cmd, **scrubbed_kwargs)
        except:
            raise HTTPError(400, "Could not start slurm jobstep on allocation. The JupyterLab log contains more information.")

        self.pid = self.process.pid
        return self.connection_info

    async def post_launch(self, **kwargs: Any) -> None:
        # Add KernelID to local user storage file
        alloc_dict = self.read_local_storage_file()
        if self.kernel_id not in alloc_dict[self.alloc_id]["kernel_ids"]:
            alloc_dict[self.alloc_id]["kernel_ids"].append(self.kernel_id)
            self.write_local_storage_file(alloc_dict)
        return await super().post_launch(**kwargs)

    @staticmethod
    def _scrub_kwargs(kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """Remove any keyword arguments that Popen does not tolerate."""
        keywords_to_scrub: List[str] = ["extra_arguments", "kernel_id"]
        scrubbed_kwargs = kwargs.copy()
        for kw in keywords_to_scrub:
            scrubbed_kwargs.pop(kw, None)
        return scrubbed_kwargs

    async def get_provisioner_info(self) -> Dict:
        """Captures the base information necessary for persistence relative to this instance."""
        provisioner_info = await super().get_provisioner_info()
        provisioner_info.update(
            {
                "alloc_id": self.alloc_id,
                "listnode": self.alloc_listnode,
                "node": self.node,
            }
        )
        return provisioner_info

    async def load_provisioner_info(self, provisioner_info: Dict) -> None:
        """Loads the base information necessary for persistence relative to this instance."""
        await super().load_provisioner_info(provisioner_info)
        self.alloc_id = provisioner_info["alloc_id"]
        self.alloc_listnode = provisioner_info["listnode"]
        self.node = provisioner_info["node"]
