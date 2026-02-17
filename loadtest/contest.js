// Load test for real-time contests: k6 run loadtest/contest.js
//   docker run --rm -i -e BASE_URL=http://host.docker.internal:8000 -v ${PWD}/loadtest:/scripts grafana/k6 run /scripts/contest.js
//
// Setup creates a fresh school + live contest. Then two scenarios run at once:
//   contestants: students register, start the contest, answer, submit, then poll the leaderboard
//   watchers:    students hold the live WebSocket and count pushed leaderboard updates
import http from "k6/http";
import ws from "k6/ws";
import { check, fail, sleep } from "k6";
import { Counter, Trend } from "k6/metrics";

const BASE = (__ENV.BASE_URL || "http://localhost:8000").replace(/\/$/, "");
const API = `${BASE}/api/v1`;
const WATCHERS = Number(__ENV.WATCHERS || 20);
const PEAK = Number(__ENV.PEAK_VUS || 50);
const RUN = `${Date.now().toString(36)}`;
const PASSWORD = "LoadTest123!";

const boardUpdates = new Counter("ws_board_updates");
const wsConnectMs = new Trend("ws_connect_ms", true);
const submitToRank = new Trend("submit_to_rank_ms", true);

export const options = {
  setupTimeout: "120s",
  scenarios: {
    contestants: {
      executor: "ramping-vus",
      exec: "contestant",
      startVUs: 0,
      stages: [
        { duration: "20s", target: Math.ceil(PEAK / 2) },
        { duration: "40s", target: PEAK },
        { duration: "20s", target: 0 },
      ],
      gracefulRampDown: "10s",
    },
    watchers: {
      executor: "per-vu-iterations",
      exec: "watcher",
      vus: WATCHERS,
      iterations: 1,
      maxDuration: "2m",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    "http_req_duration{name:start}": ["p(95)<500"],
    "http_req_duration{name:submit}": ["p(95)<500"],
    "http_req_duration{name:leaderboard}": ["p(95)<200"],
    ws_board_updates: ["count>0"],
    checks: ["rate>0.99"],
  },
};

const json = (token) => ({ headers: { "Content-Type": "application/json", ...(token ? { Authorization: `Bearer ${token}` } : {}) } });

function registerStudent(joinCode, label) {
  const r = http.post(
    `${API}/auth/register`,
    JSON.stringify({ join_code: joinCode, full_name: `Load ${label}`, email: `lt-${RUN}-${label}@load.eduhub.dev`, password: PASSWORD, stream: "natural" }),
    { ...json(), tags: { name: "register" } },
  );
  if (!check(r, { "registered": (x) => x.status === 201 })) fail(`register ${r.status} ${r.body}`);
  return r.json("access_token");
}

export function setup() {
  const org = http.post(
    `${API}/auth/organizations`,
    JSON.stringify({ organization_name: `Load School ${RUN}`, full_name: "Load Admin", email: `lt-${RUN}-admin@load.eduhub.dev`, password: PASSWORD }),
    json(),
  );
  if (org.status !== 201) fail(`create org ${org.status} ${org.body}`);
  const admin = org.json("access_token");
  const joinCode = http.get(`${API}/organizations/me`, json(admin)).json("join_code");

  const now = Date.now();
  const contest = http.post(
    `${API}/contests`,
    JSON.stringify({
      title: `Load test ${RUN}`,
      starts_at: new Date(now - 5_000).toISOString(),
      ends_at: new Date(now + 30 * 60_000).toISOString(),
      duration_minutes: 10,
      question_count: 10,
    }),
    json(admin),
  );
  if (contest.status !== 201) fail(`create contest ${contest.status} ${contest.body}`);

  const watchers = [];
  for (let i = 0; i < WATCHERS; i++) watchers.push(registerStudent(joinCode, `w${i}`));
  return { contestId: contest.json("id"), joinCode, watchers };
}

export function contestant(data) {
  const token = registerStudent(data.joinCode, `c${__VU}-${__ITER}`);
  const auth = json(token);

  const play = http.post(`${API}/contests/${data.contestId}/start`, null, { ...auth, tags: { name: "start" } });
  check(play, { "started": (r) => r.status === 200 });
  if (play.status !== 200) return;

  sleep(1 + Math.random() * 2); // "thinking"
  const answers = play.json("questions").map((q) => ({ question_id: q.id, choice_id: q.choices[Math.floor(Math.random() * q.choices.length)].id }));
  const t0 = Date.now();
  const submit = http.post(`${API}/contests/${data.contestId}/submit`, JSON.stringify({ answers }), { ...auth, tags: { name: "submit" } });
  check(submit, { "submitted": (r) => r.status === 200 && r.json("my_entry.score") !== null });

  const board = http.get(`${API}/contests/${data.contestId}/leaderboard`, { ...auth, tags: { name: "leaderboard" } });
  check(board, { "ranked on board": (r) => r.status === 200 && r.json("me") !== null });
  if (board.status === 200 && board.json("me")) submitToRank.add(Date.now() - t0);

  for (let i = 0; i < 3; i++) {
    sleep(2);
    http.get(`${API}/contests/${data.contestId}/leaderboard`, { ...auth, tags: { name: "leaderboard" } });
  }
}

export function watcher(data) {
  const token = data.watchers[(__VU - 1) % data.watchers.length];
  const t = http.post(`${API}/contests/${data.contestId}/live-ticket`, null, { ...json(token), tags: { name: "live-ticket" } });
  if (!check(t, { "ticket issued": (r) => r.status === 200 })) return;

  const url = `${BASE.replace(/^http/, "ws")}/api/v1/contests/${data.contestId}/live?ticket=${encodeURIComponent(t.json("ticket"))}`;
  const opened = Date.now();
  const res = ws.connect(url, null, (socket) => {
    socket.on("open", () => wsConnectMs.add(Date.now() - opened));
    socket.on("message", (raw) => {
      const msg = JSON.parse(raw);
      if (msg.type === "board") boardUpdates.add(1);
    });
    socket.setTimeout(() => socket.close(), 90_000);
  });
  check(res, { "ws upgraded": (r) => r && r.status === 101 });
}
