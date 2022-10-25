from setuptools import setup

setup(
    name="slurm_provisioner",
    version="0.1.0",
    description="Jupyter slurm kernel provisioner",
    url="https://github.com/FZJ-JSC/jupyter-slurm-provisioner",
    author="Tim Kreuzer",
    author_email="t.kreuzer@fz-juelich.de",
    license="MIT",
    packages=["slurm_provisioner"],
    install_requires=["jupyter_client>=7.1.2"],
    entry_points={
        "jupyter_client.kernel_provisioners": [
            "slurm-provisioner = slurm_provisioner:SlurmProvisioner",
        ]
    },
    scripts=[
        "scripts/slurmel_allocate",
        "scripts/slurmel_allocinfo",
        "scripts/slurmel_cancel",
        "scripts/slurmel_jobinfo",
        "scripts/slurmel_kernel_start",
    ],
)
