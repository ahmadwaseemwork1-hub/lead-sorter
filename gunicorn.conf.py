# Gunicorn picks this up automatically when it starts from the repo root
# (Render does). It applies as long as the start command doesn't pass the
# same flags, so the settings are right even on a Web Service whose start
# command wasn't updated.
#
# Organizing a big workbook is a long, CPU-bound request: a few thousand
# leads spread over a dozen differently-shaped sheets can run for minutes on
# a small instance. With the default sync worker that request blocks the
# whole worker, so gunicorn's liveness timeout kills it mid-flight and the
# upload comes back as a bare "Internal Server Error". Threads fix both
# halves of that: the long upload runs in its own thread (so it is allowed
# to finish) while other people can still load the page.
worker_class = "gthread"
workers = 1
threads = 4
timeout = 900
