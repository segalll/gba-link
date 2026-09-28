const token = new URLSearchParams(location.hash.slice(1)).get("token");
history.replaceState(null, "", location.pathname);
const screen = document.querySelector("#screen");
const status = document.querySelector("#status");
const reconnect = document.querySelector("#connect");
let peer,
  channel,
  keys = 0,
  touches = 0,
  timer;
const mapping = {
  KeyX: 0,
  KeyZ: 1,
  ShiftLeft: 2,
  ShiftRight: 2,
  Enter: 3,
  ArrowRight: 4,
  ArrowLeft: 5,
  ArrowUp: 6,
  ArrowDown: 7,
  KeyS: 8,
  KeyA: 9,
};

async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok)
    throw new Error(
      response.status === 401
        ? "This room has ended. Rejoin from RomM."
        : "Could not connect to the room.",
    );
  return response.json();
}

function send() {
  let mask = keys | touches;
  if (document.visibilityState === "visible" && document.hasFocus()) {
    for (const pad of navigator.getGamepads?.() ?? []) {
      if (!pad || pad.mapping !== "standard") continue;
      const buttons = [0, 1, 8, 9, 15, 14, 12, 13, 5, 4];
      buttons.forEach((button, bit) => {
        if (pad.buttons[button]?.pressed) mask |= 1 << bit;
      });
      if (pad.axes[0] > 0.5) mask |= 1 << 4;
      if (pad.axes[0] < -0.5) mask |= 1 << 5;
      if (pad.axes[1] < -0.5) mask |= 1 << 6;
      if (pad.axes[1] > 0.5) mask |= 1 << 7;
    }
  }
  if (channel?.readyState === "open" && channel.bufferedAmount < 256) {
    channel.send(new Uint8Array([mask & 255, mask >> 8]));
  }
}

for (const event of ["keydown", "keyup"]) {
  window.addEventListener(event, (e) => {
    const bit = mapping[e.code];
    if (bit === undefined) return;
    if (
      event === "keydown" &&
      e.code === "Enter" &&
      e.target.closest("button, summary")
    )
      return;
    e.preventDefault();
    if (event === "keydown") keys |= 1 << bit;
    else keys &= ~(1 << bit);
    send();
  });
}
function clear() {
  keys = touches = 0;
  send();
}
window.addEventListener("blur", clear);
document.addEventListener("visibilitychange", clear);
for (const button of document.querySelectorAll("[data-key]")) {
  const bit = 1 << Number(button.dataset.key);
  button.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    button.setPointerCapture(e.pointerId);
    touches |= bit;
    send();
  });
  for (const event of ["pointerup", "pointercancel", "lostpointercapture"]) {
    button.addEventListener(event, () => {
      touches &= ~bit;
      send();
    });
  }
}
document.querySelector("#sound").addEventListener("click", async () => {
  screen.muted = false;
  await screen.play();
  screen.focus();
});

async function connect() {
  reconnect.hidden = true;
  clearInterval(timer);
  if (peer) {
    peer.onconnectionstatechange = null;
    peer.close();
  }
  try {
    let context;
    do {
      context = await api("context");
      status.textContent = context.waiting
        ? "Waiting for player two to join from RomM..."
        : "Connecting...";
      if (context.waiting)
        await new Promise((resolve) => setTimeout(resolve, 1500));
    } while (context.waiting);
    peer = new RTCPeerConnection({ iceServers: context.iceServers });
    const stream = new MediaStream();
    screen.srcObject = stream;
    screen.muted = true;
    peer.ontrack = (event) => {
      stream.addTrack(event.track);
      screen.play().catch(() => {});
    };
    peer.onconnectionstatechange = () => {
      if (peer.connectionState === "connected") {
        status.textContent = `Player ${context.player} connected.`;
        screen.focus();
      }
      if (["disconnected", "failed", "closed"].includes(peer.connectionState)) {
        clear();
        status.textContent = "Connection lost. The game is paused.";
        reconnect.hidden = false;
      }
    };
    peer.addTransceiver("video", { direction: "recvonly" });
    peer.addTransceiver("audio", { direction: "recvonly" });
    channel = peer.createDataChannel("inputs", {
      ordered: false,
      maxRetransmits: 0,
    });
    channel.onopen = () => {
      send();
      timer = setInterval(send, 1000 / 120);
    };
    await peer.setLocalDescription(await peer.createOffer());
    if (peer.iceGatheringState !== "complete")
      await new Promise((resolve, reject) => {
        const timeout = setTimeout(
          () => reject(new Error("Connection setup timed out.")),
          15000,
        );
        peer.addEventListener("icegatheringstatechange", () => {
          if (peer.iceGatheringState === "complete") {
            clearTimeout(timeout);
            resolve();
          }
        });
      });
    await peer.setRemoteDescription(await api("offer", peer.localDescription));
  } catch (error) {
    status.textContent = error.message;
    reconnect.hidden = false;
  }
}
reconnect.addEventListener("click", connect);
window.addEventListener("pagehide", () => {
  clearInterval(timer);
  clear();
  peer?.close();
});
connect();
