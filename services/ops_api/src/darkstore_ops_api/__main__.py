from darkstore_common.runner import main

from .service import serve

main(serve, default_port=8003)
