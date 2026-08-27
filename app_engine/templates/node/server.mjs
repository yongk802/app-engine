import http from "node:http";

const port = Number.parseInt(process.env.PORT ?? "8000", 10);
const server = http.createServer((request, response) => {
  const ready = request.url === "/" || request.url === "/health";
  const body = JSON.stringify({app: "{{APP_ID}}", ready});
  response.writeHead(ready ? 200 : 404, {
    "content-type": "application/json",
    "content-length": Buffer.byteLength(body),
  });
  response.end(body);
});

server.listen(port, "127.0.0.1", () => {
  console.log(`{{APP_LABEL}} listening on ${port}`);
});
