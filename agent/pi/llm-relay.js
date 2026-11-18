// Inside-container relay: 127.0.0.1:<port> -> unix socket mounted by the orchestrator.
// The container has no network; the only reachable endpoint is this task-scoped socket, and the
// host side of the socket (orchestrator/llm_relay.py) filters which model API routes are allowed.
const net = require("net");

const sock = process.env.LLM_SOCKET || "/run/llm/llm.sock";
const port = parseInt(process.env.LLM_PORT || "11434", 10);

net
  .createServer((client) => {
    const upstream = net.connect(sock);
    client.pipe(upstream).pipe(client);
    const close = () => { client.destroy(); upstream.destroy(); };
    client.on("error", close);
    upstream.on("error", close);
  })
  .listen(port, "127.0.0.1");
