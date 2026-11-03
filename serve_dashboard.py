#!/usr/bin/env python3
import http.server, socketserver, os, urllib.parse
PORT = 7890
DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(DIR)

class Handler(http.server.SimpleHTTPRequestHandler):
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", ""):
            self.path = "/dashboard.html"
        super().do_GET()

with socketserver.TCPServer(("", PORT), Handler) as httpd:
    httpd.allow_reuse_address = True
    print("Dashboard on port %d" % PORT)
    httpd.serve_forever()
