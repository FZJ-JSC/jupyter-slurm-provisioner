import tornado
from notebook.notebookapp import NotebookApp

# Update this import to reflect your package name
# from jupyter_slurm_provisioner import load_jupyter_server_extension

# Starts a local server, used to attach a debugger
if __name__ == "__main__":
    notebookapp = NotebookApp()
    # Initialise config file and setup the app
    notebookapp.initialize()
    
    # Load the handlers
    # load_jupyter_server_extension(notebookapp)

    # Start tornado server
    notebookapp.web_app.listen(8123)
    tornado.ioloop.IOLoop.instance().start()