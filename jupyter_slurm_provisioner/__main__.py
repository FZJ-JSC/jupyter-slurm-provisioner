from jupyter_server import serverapp as app

# Update this import to reflect your package name
# from jupyter_slurm_provisioner_extension import load_jupyter_server_extension

# from jupyter_slurm_provisioner import CustomAsyncMappingKernelManager
# Starts a local server, used to attach a debugger
if __name__ == "__main__":
    server = app.launch_new_instance()
