"""Synthetic session verifier for an internal-only test network. No real users."""
import http.server
import json


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        cookie = self.headers.get('Cookie','')
        role = 'admin' if cookie == 'synthetic=admin' else 'user' if cookie == 'synthetic=user' else None
        self.send_response(200 if role else 401)
        self.send_header('Content-Type','application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'role':role}).encode())


http.server.HTTPServer(('0.0.0.0',3000),Handler).serve_forever()
