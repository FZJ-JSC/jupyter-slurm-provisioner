import logging
import tornado
from tornado.log import LogFormatter
from notebook.notebookapp import NotebookApp

# Update this import to reflect your package name
# from jupyter_slurm_provisioner import load_jupyter_server_extension

# Starts a local server, used to attach a debugger
if __name__ == "__main__":
    notebookapp = NotebookApp()
    # Initialise config file and setup the app
    notebookapp.initialize()
    logger = notebookapp.log
    x = LogFormatter(fmt="%(color)s[%(levelname)1.1s %(asctime)s.%(msecs).03d %(name)s %(pathname)s:%(lineno)d]%(end_color)s %(message)s")
    
    from tornado.log import LogFormatter
    for h in logger.handlers:
        h.formatter = x
        # h.format = "%(color)s[%(levelname)1.1s %(asctime)s.%(msecs).03d %(name)s %(filename)s]%(end_color)s ABC %(message)s"
    
    # Start tornado server
    notebookapp.web_app.listen(8123)
    tornado.ioloop.IOLoop.instance().start()