const express = require('express');
const path = require('path');
const fs = require('fs');
const http = require('http');
const https = require('https');
const { createProxyMiddleware } = require('http-proxy-middleware');

const app = express();
const PORT = 3000;
const HOST = '0.0.0.0';
const BACKEND_HTTP = 'http://127.0.0.1:8000';
const MIC_WS = 'ws://127.0.0.1:8765';
const SSL_CERT_FILE = process.env.SSL_CERT_FILE || path.join(__dirname, 'certs', 'cert.pem');
const SSL_KEY_FILE = process.env.SSL_KEY_FILE || path.join(__dirname, 'certs', 'key.pem');

const backendProxy = createProxyMiddleware({
  target: BACKEND_HTTP,
  changeOrigin: true,
  ws: true,
});

const micProxy = createProxyMiddleware({
  target: MIC_WS,
  changeOrigin: true,
  ws: true,
});

app.use('/socket.io', (req, res, next) => {
  req.url = '/socket.io' + req.url;
  next();
}, backendProxy);

app.get('/video_feed', (req, res, next) => {
  req.url = '/video_feed';
  backendProxy(req, res, next);
});

app.get('/video_feed/', (req, res, next) => {
  req.url = '/video_feed';
  backendProxy(req, res, next);
});

app.use('/mic', (req, res, next) => {
  req.url = '/mic' + req.url;
  next();
}, micProxy);

app.use(express.static(path.join(__dirname)));

app.get('/', (req, res) => {
  res.sendFile(path.join(__dirname, 'index.html'));
});

let server;
if (fs.existsSync(SSL_CERT_FILE) && fs.existsSync(SSL_KEY_FILE)) {
  const credentials = {
    cert: fs.readFileSync(SSL_CERT_FILE),
    key: fs.readFileSync(SSL_KEY_FILE),
  };
  server = https.createServer(credentials, app);
  server.listen(PORT, HOST, () => {
    console.log(`Frontend HTTPS server running at https://localhost:${PORT}`);
  });
} else {
  server = http.createServer(app);
  server.listen(PORT, HOST, () => {
    console.log(`Frontend HTTP server running at http://localhost:${PORT}`);
    console.log(`HTTPS disabled: missing ${SSL_CERT_FILE} or ${SSL_KEY_FILE}`);
  });
}

server.on('upgrade', (req, socket, head) => {
  if (req.url.startsWith('/socket.io')) {
    backendProxy.upgrade(req, socket, head);
    return;
  }
  if (req.url.startsWith('/mic')) {
    micProxy.upgrade(req, socket, head);
  }
});
