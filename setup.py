from setuptools import setup

try:
    with open("README") as f:
        long_description = f.read()
except Exception:
    long_description = ""

setup(
    name="jupyter-slurm-provisioner",
    version="0.4.3",
    description="Jupyter slurm kernel provisioner",
    url="https://github.com/FZJ-JSC/jupyter-slurm-provisioner",
    author="Tim Kreuzer",
    author_email="t.kreuzer@fz-juelich.de",
    license="MIT",
    packages=["jupyter_slurm_provisioner"],
    install_requires=["jupyter_client>=7.1.2"],
    long_description=long_description,
    entry_points={
        "jupyter_client.kernel_provisioners": [
            "slurm-provisioner = jupyter_slurm_provisioner:SlurmProvisioner",
        ]
    },
    scripts=[
        "scripts/slurm_watch",
        "scripts/slurmel_allocate",
        "scripts/slurmel_allocinfo",
        "scripts/slurmel_allocwait",
        "scripts/slurmel_cancel",
        "scripts/slurmel_jobinfo",
        "scripts/slurmel_kernel_start",
        "scripts/slurmel_update_state",
    ],
)
