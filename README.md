# Jupyter slurm provisioner

## Overview
The slurm provisioner is a subclass of [`jupyter_client.KernelProvisionerBase`](https://github.com/jupyter/jupyter_client/blob/v7.4.2/jupyter_client/provisioning/provisioner_base.py#L24). Its area of use is any slurm-based HPC system. However, it was written to be used within the Juelich Supercomputing Centre, which uses a script called `jutil` to create a user-specific environment based on a project. You may have to update `scripts/slurmel_allocate` to use it on any other HPC system.
It allows users to start their jupyter kernel on any partition, while their notebook server is running on an interactive login node.
This offers a separation between code execution and user interface. The slurm provisioner will not use its ipykernel argv configuration
but is meant to be a wrapper for any existing kernel. It is recommended to use this kernel with the option [`--KernelRestarter.restart_limit=0`](https://github.com/jupyter/jupyter_client/blob/v7.4.2/jupyter_client/restarter.py#L43) to avoid unsought compute time and costs.

## Configuration
Configure a kernel.json file in your `$HOME`. It is not recommended to define the kernel globally, since the configuration is user-specific.

### Options
 * project [required]: Used for correct budgeting of compute time.
 * partition [required]: specify the slurm partition
 * nodes [required]: specify the number of nodes
 * runtime [required]: specify the runtime in minutes
 * kernel_argv [required]: copy & paste this from the kernel you want to run
 * gpus [optional]: specify the number of GPUs, if the partition supports GPUs
 * reservation [optional]: specify the slurm reservation, if you have one
 * jobid [optional]: specify a pre-existing slurm allocation and start your kernel there. Without this, a new allocation for each kernel will be created.
 * node [optional]: specify a node in your pre-existing allocation

### Example `kernel.json`
`.local/share/jupyter/kernels/slurmel/kernel.json`
```
{
    "display_name": "Slurm Kernel",
    "language": "python",
    "metadata": {
        "debugger": true,
        "kernel_provisioner": {
            "config": {
                "gpus": "0",
                "nodes": "2",
                "partition": "batch",
                "project": "...",
                "reservation": "None",
                "runtime": 30,
                "jobid": "None",
                "node": "None",
                "kernel_argv": [
                    "~/.local/share/jupyter/kernels/my_kernel/kernel.sh",
                    "-f",
                    "{connection_file}"
                ]
            },
            "provisioner_name": "slurm-provisioner"
        }
    }
}
```

## Restart behavior
Whenever you stop the last kernel on an existing slurm allocation, this allocation will be relinquished.
This is also the case if you "restart" the last/only kernel on this allocation.

To avoid an unwanted lost of an allocation, you might want to start a second kernel on your allocation, using the `jobid` configuration.
