# Gunicorn picks this up automatically when it starts from the repo root
# (Render does). It applies as long as the start command doesn't pass an
# explicit --timeout, so the timeout is right even on a Web Service whose
# start command wasn't updated. Organizing + scoring a multi-thousand-row
# upload on a small/free instance can take longer than gunicorn's 30s
# default, which would otherwise kill the worker and 500 the upload.
timeout = 180
workers = 1
