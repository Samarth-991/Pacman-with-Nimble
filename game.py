"""
Pac-Man where the four ghosts are driven by Ollama Nimble (/v1/systemone).

You play Pac-Man. Every ~0.15 s a background thread sends Nimble the full game
state - maze tile AND pixel coordinates of Pac-Man and of every ghost, each
ghost's legal moves, and Pac-Man's heading - plus one "question" per ghost
(blinky_move, pinky_move, inky_move, clyde_move). Nimble answers with a
direction + probabilities for each. The game loop never waits on Ollama: it
uses the latest cached answer when a ghost reaches a junction, and falls back
to a classic rule-based AI if Nimble is offline, stale, or suggests an illegal move.

Based on the pattern of your Nimble square-chase game.
Requires: pygame, `ollama serve`, and your Nimble model pulled.

Modes (python game.py --help):
  --mode nimble   you play Pac-Man, Nimble drives the ghosts          (default)
  --mode auto     Pac-Man plays itself (autopilot); ghosts use Nimble. Pac-Man's own
                  brain is Nimble (--pac-brain nimble) or rules (--pac-brain rules).
                  Press any arrow/WASD key to take over.
  --mode rules    you play, ghosts use the built-in rule AI (no Ollama needed)

Keys: Arrows / WASD = move | A = toggle autopilot | N = toggle Nimble ON/OFF
      C = coordinate overlay | R = restart | Esc = quit
"""
import argparse
import json
import math
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque

import pygame

# ----------------------------------------------------------------- Nimble cfg
OLLAMA_SYSTEMONE = "http://127.0.0.1:11434/v1/systemone"
NIMBLE_MODEL = "nimble-v3:9b-q4_K_M"
DECISION_INTERVAL_S = 0.15   # minimum gap between Nimble requests
STALE_AFTER_S = 1.5          # ignore Nimble answers older than this
PROBE_QUESTION = {"ready": {"type": "choice", "instructions": "Answer yes.",
                            "criteria": {"yes": "ready", "no": "not ready"}}}
REQUEST_TIMEOUT_S = 30

# ------------------------------------------------------------------ game cfg
FPS = 60
T = 24                       # tile size in px
PAC_SPEED = 2                # px / frame  (use 1,2,3,4,6 - must divide 12)
GHOST_SPEED = 2
FRIGHT_SPEED = 1
EATEN_SPEED = 4
FRIGHT_SECONDS = 7.0
START_LIVES = 3

MAZE = [
    "###################",
    "#........#........#",
    "#o##.###.#.###.##o#",
    "#.................#",
    "#.##.#.#####.#.##.#",
    "#....#...#...#....#",
    "####.### # ###.####",
    "####.#       #.####",
    "####.# ##-## #.####",
    "    .  #   #  .    ",
    "####.# ##### #.####",
    "####.#       #.####",
    "####.# ##### #.####",
    "#........#........#",
    "#.##.###.#.###.##.#",
    "#o.#.....P.....#.o#",
    "##.#.#.#####.#.#.##",
    "#....#...#...#....#",
    "#.######.#.######.#",
    "#.................#",
    "###################",
]
COLS, ROWS = len(MAZE[0]), len(MAZE)
PANEL_W = 330
TOP = 44
WIDTH = COLS * T + PANEL_W
HEIGHT = ROWS * T + TOP

HOUSE_TILE = (9, 9)          # middle of the ghost house
EXIT_TILE = (9, 7)           # just above the ghost-house door

DIRS = {"left": (-1, 0), "right": (1, 0), "up": (0, -1), "down": (0, 1)}
NAME_OF = {v: k for k, v in DIRS.items()}
OPPOSITE = {"left": "right", "right": "left", "up": "down", "down": "up"}
TIE_ORDER = ["up", "left", "down", "right"]

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
WALL_FILL = (10, 10, 70)
WALL_EDGE = (50, 80, 255)
PELLET = (255, 220, 180)
PAC_YELLOW = (255, 235, 0)
FRIGHT_BLUE = (30, 40, 255)

GHOST_DEFS = [
    # name,   colour,           spawn tile, release delay (s)
    ("blinky", (255, 0, 0), (9, 7), 0.0),
    ("pinky", (255, 150, 255), (9, 9), 3.0),
    ("inky", (0, 230, 255), (8, 9), 6.0),
    ("clyde", (255, 170, 60), (10, 9), 9.0),
]

PERSONALITY = {
    "blinky": "You are Blinky, the red ghost. Chase Pac-Man directly: pick the move that "
              "closes the distance to his current tile fastest.",
    "pinky": "You are Pinky, the pink ghost. Ambush Pac-Man: head for the tile 4 tiles "
             "ahead of the direction he is facing, not his current tile.",
    "inky": "You are Inky, the cyan ghost. Flank Pac-Man: approach from the opposite side "
            "to Blinky so Pac-Man gets pinched between you and Blinky.",
    "clyde": "You are Clyde, the orange ghost. Chase Pac-Man while you are more than 8 tiles "
             "away, but retreat toward the bottom-left corner (tile col 1,row 19) when "
             "closer than 8 tiles.",
}
PACMAN_PROMPT = (
    "You are Pac-Man, controlled by an autopilot. Eat every pellet and stay alive. Never move "
    "toward a ghost whose state is not 'frightened' if it is within 3 tiles; flee along the route "
    "with the most free space instead. If a ghost is 'frightened' and close, chase it. Otherwise "
    "head for the nearest pellet, preferring a power pellet when ghosts are near. "
    "Coordinates are maze tiles (col,row): col grows right, row grows downward. "
    "Your data is state.pacman. Choose ONLY a direction whose can_move flag is true."
)
COMMON_RULES = (
    "Coordinates are maze tiles (col,row): col grows to the right, row grows downward. "
    "Your own data is in state.ghosts.<your name>. Choose ONLY a direction whose can_move "
    "flag is true, and avoid reversing your current direction unless forced. "
    "If your state is 'frightened', do the opposite: maximise distance from Pac-Man."
)

screen = FONT = FONT_B = FONT_S = FONT_BIG = None


def init_display():
    global screen, FONT, FONT_B, FONT_S, FONT_BIG
    pygame.init()
    screen = pygame.display.set_mode((WIDTH, HEIGHT), pygame.SCALED | pygame.RESIZABLE)
    pygame.display.set_caption("Pac-Man (Nimble ghosts)")
    FONT = pygame.font.SysFont("monospace", 15)
    FONT_B = pygame.font.SysFont("monospace", 17, bold=True)
    FONT_S = pygame.font.SysFont("monospace", 11)
    FONT_BIG = pygame.font.SysFont(None, 56)


# ------------------------------------------------------------------- maze
class Maze:
    def __init__(self):
        self.pellets, self.power = set(), set()
        self.pac_start = (9, 15)
        for r, row in enumerate(MAZE):
            for c, ch in enumerate(row):
                if ch == ".":
                    self.pellets.add((c, r))
                elif ch == "o":
                    self.power.add((c, r))
                elif ch == "P":
                    self.pac_start = (c, r)
        self.total_pellets = len(self.pellets) + len(self.power)

    @staticmethod
    def char(c, r):
        if r < 0 or r >= ROWS:
            return "#"
        return MAZE[r][c % COLS]          # columns wrap -> side tunnel

    def passable(self, c, r, door_ok=False):
        ch = self.char(c, r)
        return ch != "#" and (ch != "-" or door_ok)

    def options(self, c, r, door_ok=False):
        return [n for n, (dx, dy) in DIRS.items() if self.passable(c + dx, r + dy, door_ok)]

    def bfs_dir(self, start, goal, door_ok=True):
        """First move of the shortest path start -> goal (None if already there)."""
        q, seen = deque([(start, None)]), {start}
        while q:
            (c, r), first = q.popleft()
            if (c, r) == goal:
                return first
            for name, (dx, dy) in DIRS.items():
                n = ((c + dx) % COLS, r + dy)
                if n not in seen and self.passable(n[0], n[1], door_ok):
                    seen.add(n)
                    q.append((n, first or name))
        return None


    def dist_map(self, sources, limit=99):
        """Walking distance (tiles) from the nearest source to every reachable tile."""
        dist = {s: 0 for s in sources}
        q = deque(sources)
        while q:
            c, r = q.popleft()
            if dist[(c, r)] >= limit:
                continue
            for dx, dy in DIRS.values():
                n = ((c + dx) % COLS, r + dy)
                if n not in dist and self.passable(n[0], n[1]):
                    dist[n] = dist[(c, r)] + 1
                    q.append(n)
        return dist

    def path_first_move(self, start, goals, avoid=lambda t: False):
        """First move of the shortest path from start to any goal tile, skipping avoided tiles."""
        q, seen = deque([(start, None)]), {start}
        while q:
            (c, r), first = q.popleft()
            if first and (c, r) in goals:
                return first
            for name, (dx, dy) in DIRS.items():
                n = ((c + dx) % COLS, r + dy)
                if n not in seen and self.passable(n[0], n[1]) and not avoid(n):
                    seen.add(n)
                    q.append((n, first or name))
        return None


# ----------------------------------------------------------------- actors
class Actor:
    def __init__(self, tile):
        self.spawn = tile
        self.reset()

    def reset(self):
        self.x = self.spawn[0] * T + T // 2      # pixel centre of the actor
        self.y = self.spawn[1] * T + T // 2
        self.dir = (0, 0)

    @property
    def tile(self):
        return (self.x // T) % COLS, self.y // T

    @property
    def pixel(self):
        return self.x, self.y

    def at_center(self):
        return self.x % T == T // 2 and self.y % T == T // 2

    def advance(self):
        self.x = (self.x + self.dir[0]) % (COLS * T)
        self.y += self.dir[1]


class Pacman(Actor):
    def reset(self):
        super().reset()
        self.facing = (-1, 0)
        self.mouth = 0

    def update(self, maze, wanted, chooser=None):
        """chooser: autopilot callback, called at every tile centre instead of reading keys."""
        for _ in range(PAC_SPEED):
            if chooser is None and self.dir != (0, 0) and wanted == (-self.dir[0], -self.dir[1]):
                self.dir = wanted                     # instant U-turn (human only)
            if self.at_center():
                if chooser:
                    wanted = chooser()
                c, r = self.tile
                if wanted != (0, 0) and maze.passable(c + wanted[0], r + wanted[1]):
                    self.dir = wanted
                if self.dir != (0, 0) and not maze.passable(c + self.dir[0], r + self.dir[1]):
                    self.dir = (0, 0)
            self.advance()
        if self.dir != (0, 0):
            self.facing = self.dir
            self.mouth += 1


class Ghost(Actor):
    def __init__(self, name, color, spawn, release_s):
        self.name, self.color, self.release_s = name, color, release_s
        super().__init__(spawn)

    def reset(self, base_t=0.0):
        super().reset()
        self.mode = "normal" if self.name == "blinky" else "house"   # house|leaving|normal|eaten
        self.frightened = False
        self.release_at = base_t + self.release_s
        self.source = "-"            # who made the last decision: nimble / rules / forced

    @property
    def state_name(self):
        return "frightened" if self.frightened and self.mode != "eaten" else self.mode

    @property
    def speed(self):
        if self.mode == "house":
            return 0
        if self.mode == "eaten":
            return EATEN_SPEED
        if self.mode == "leaving":
            return GHOST_SPEED
        return FRIGHT_SPEED if self.frightened else GHOST_SPEED

    def update(self, game):
        for _ in range(self.speed):
            if self.at_center():
                self.decide(game)
            self.advance()

    def decide(self, game):
        c, r = self.tile
        maze = game.maze
        if self.mode == "house":
            self.dir = (0, 0)
            return
        if self.mode == "leaving":
            if (c, r) != EXIT_TILE:
                d = maze.bfs_dir((c, r), EXIT_TILE)
                self.dir = DIRS[d] if d else (0, 0)
                return
            self.mode = "normal"
        if self.mode == "eaten":
            if (c, r) == HOUSE_TILE:
                self.mode, self.frightened = "house", False
                self.release_at = game.t + 2.0
                self.dir = (0, 0)
                return
            self.dir = DIRS[maze.bfs_dir((c, r), HOUSE_TILE)]
            return
        self.dir = DIRS[game.choose_ghost_move(self)]


# ---------------------------------------------------------- Nimble parser
def parse_nimble_answers(data, expected):
    """Validate and normalise a /v1/systemone reply.

    expected: question keys we asked (e.g. "blinky_move").
    Returns {question_key: {"choice": "left|right|up|down", "probs": {dir: float}}}.
    - choice is lower-cased/stripped; if it is missing or invalid, the highest
      probability direction is used instead
    - probabilities with unknown directions / non-numeric values are dropped
    - questions with no usable answer are simply omitted (the game falls back to rules)
    Raises ValueError if the reply has no 'answers' object at all.
    """
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
        raise ValueError("Nimble reply has no 'answers' object")
    out = {}
    for q in expected:
        ans = data["answers"].get(q)
        if not isinstance(ans, dict):
            continue
        probs = {}
        raw = ans.get("probabilities")
        if isinstance(raw, dict):
            for k, v in raw.items():
                k = str(k).strip().lower()
                if k in DIRS and isinstance(v, (int, float)) and not isinstance(v, bool):
                    probs[k] = float(v)
        choice = ans.get("choice")
        choice = choice.strip().lower() if isinstance(choice, str) else None
        if choice not in DIRS:
            choice = max(probs, key=probs.get) if probs else None
        if choice:
            out[q] = {"choice": choice, "probs": probs}
    return out


# ------------------------------------------------------------ Nimble client
class NimbleClient:
    """Background thread: keeps the latest Nimble decision for every ghost."""

    def __init__(self, game):
        self.game = game
        self.lock = threading.Lock()
        self.cache = {}              # ghost name -> {choice, probs, t}
        self.online = False
        self.latency_ms = None
        self.last_error = ""
        self._started = False

    def start(self):
        if not self._started:
            self._started = True
            threading.Thread(target=self._worker, daemon=True).start()

    def get(self, name):
        """Latest answer if fresh enough to trust, else None."""
        with self.lock:
            a = self.cache.get(name)
        return a if a and time.time() - a["t"] <= STALE_AFTER_S else None

    def latest(self, name):
        with self.lock:
            return self.cache.get(name)

    def _post(self, state, questions, timeout=None):
        body = {"model": NIMBLE_MODEL, "state": state, "questions": questions, "keep_alive": "30m"}
        req = urllib.request.Request(
            OLLAMA_SYSTEMONE, json.dumps(body).encode(), {"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout or REQUEST_TIMEOUT_S) as resp:
            return json.load(resp)

    def _probe(self):
        """Tiny request so the game can wait for Nimble (and load the model) before starting."""
        started = time.time()
        try:
            parse_nimble_answers(self._post({"ping": True}, PROBE_QUESTION, timeout=120), PROBE_QUESTION)
            print("Nimble connected.")
            self.online, self.latency_ms = True, int((time.time() - started) * 1000)
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError) as e:
            if str(e) != self.last_error:
                print(f"Waiting for Nimble ({e}) - game starts once it answers")
            self.last_error = str(e)
            time.sleep(2)

    def _worker(self):
        while True:
            started = time.time()
            g = self.game
            if g.nimble_enabled and not self.online and g.ready:
                self._probe()
                continue
            if not g.nimble_enabled or g.state != "playing" or g.dying or g.ready:
                time.sleep(0.1)
                continue
            try:
                state, questions = g.build_state(), g.build_questions()
            except RuntimeError:     # set changed size during iteration; just retry
                continue
            if not questions:
                time.sleep(0.1)
                continue
            try:
                if g.print_state:
                    print(json.dumps(state))
                parsed = parse_nimble_answers(self._post(state, questions), questions)
                now = time.time()
                with self.lock:
                    for q, ans in parsed.items():
                        self.cache[q.replace("_move", "")] = {**ans, "t": now}
                if not self.online:
                    print("Nimble connected.")
                self.online, self.latency_ms = True, int((now - started) * 1000)
            except (urllib.error.URLError, TimeoutError, KeyError, ValueError, OSError) as e:
                if self.online or not self.last_error:
                    print(f"Nimble unavailable ({e}); ghosts use rule-based AI")
                self.online, self.last_error = False, str(e)
                time.sleep(2)
            time.sleep(max(0.0, DECISION_INTERVAL_S - (time.time() - started)))


# ------------------------------------------------------------------- game
class Game:
    def __init__(self, mode="nimble", pac_brain="nimble", print_state=False):
        self.mode, self.pac_brain, self.print_state = mode, pac_brain, print_state
        self.autopilot = mode == "auto"
        self.nimble_enabled = mode != "rules"
        self.pac_source = "human"
        self.overlay = False
        self.wall_surface = self._render_walls()
        self.nimble = NimbleClient(self)
        self.restart()
        if self.nimble_enabled:
            self.nimble.start()

    def restart(self):
        """(Re)initialise everything except the Nimble worker thread."""
        self.maze = Maze()
        self.pac = Pacman(self.maze.pac_start)
        self.ghosts = {n: Ghost(n, col, sp, rel) for n, col, sp, rel in GHOST_DEFS}
        self.score, self.lives = 0, START_LIVES
        self.t = 0.0                    # game clock (s), stops while paused/dying
        self.state = "playing"          # playing | won | gameover
        self.dying = 0
        self.ready = 90                 # "READY!" freeze at start / after death
        self.fright_until = 0.0
        self.combo = 0
        self.pac_wanted = (0, 0)

    @property
    def waiting_for_nimble(self):
        return self.nimble_enabled and not self.nimble.online

    # ---- state shipped to Nimble ---------------------------------------
    def build_state(self):
        """Everything Nimble needs, including pixel + tile coordinates."""
        pc, pr = self.pac.tile
        ghosts = {}
        for n, g in self.ghosts.items():
            c, r = g.tile
            can = self.maze.options(c, r)
            ghosts[n] = {
                "tile": [c, r],
                "pixel": [g.x, g.y + TOP],
                "direction": NAME_OF.get(g.dir, "none"),
                "state": g.state_name,
                "can_move": {d: d in can for d in DIRS},
                "reverse_direction": OPPOSITE.get(NAME_OF.get(g.dir, ""), "none"),
                "delta_to_pacman_tiles": {"dx": pc - c, "dy": pr - r},
                "distance_to_pacman_tiles": round(math.hypot(pc - c, pr - r), 1),
            }
        return {
            "maze": {"cols": COLS, "rows": ROWS, "tile_px": T, "origin_px": [0, TOP]},
            "frightened_seconds_left": round(max(0.0, self.fright_until - self.t), 1),
            "pacman": {
                "tile": [pc, pr],
                "pixel": [self.pac.x, self.pac.y + TOP],
                "direction": NAME_OF.get(self.pac.dir, "none"),
                "facing": NAME_OF[self.pac.facing],
                "can_move": {d: d in self.maze.options(pc, pr) for d in DIRS},
                "tile_4_ahead": [pc + 4 * self.pac.facing[0], pr + 4 * self.pac.facing[1]],
            },
            "ghosts": ghosts,
            "pellets_left": len(self.maze.pellets) + len(self.maze.power),
            "power_pellet_tiles": [list(p) for p in tuple(self.maze.power)],
        }

    def build_questions(self):
        """One choice-question per ghost that is currently free to roam."""
        crit = {
            "left": "Move left (col-1). Only if can_move.left is true.",
            "right": "Move right (col+1). Only if can_move.right is true.",
            "up": "Move up (row-1). Only if can_move.up is true.",
            "down": "Move down (row+1). Only if can_move.down is true.",
        }
        qs = {
            f"{n}_move": {
                "type": "choice",
                "instructions": f"{PERSONALITY[n]} {COMMON_RULES}",
                "criteria": crit,
            }
            for n, g in self.ghosts.items()
            if g.mode == "normal"
        }
        if self.autopilot and self.pac_brain == "nimble":
            qs["pacman_move"] = {"type": "choice", "instructions": PACMAN_PROMPT, "criteria": crit}
        return qs

    # ---- ghost decision logic ------------------------------------------
    def target_tile(self, g):
        pac, pf = self.pac.tile, self.pac.facing
        if g.name == "pinky":
            return pac[0] + 4 * pf[0], pac[1] + 4 * pf[1]
        if g.name == "inky":
            b = self.ghosts["blinky"].tile
            ahead = (pac[0] + 2 * pf[0], pac[1] + 2 * pf[1])
            return 2 * ahead[0] - b[0], 2 * ahead[1] - b[1]
        if g.name == "clyde" and math.hypot(pac[0] - g.tile[0], pac[1] - g.tile[1]) <= 8:
            return 1, 19
        return pac

    def rule_move(self, g, legal):
        pac = self.pac.tile
        c, r = g.tile
        target = self.target_tile(g)

        def score(d):
            nc, nr = c + DIRS[d][0], r + DIRS[d][1]
            if g.frightened:                       # flee: bigger distance is better
                return -math.hypot(nc - pac[0], nr - pac[1])
            return math.hypot(nc - target[0], nr - target[1])

        return min(sorted(legal, key=TIE_ORDER.index), key=score)

    def choose_ghost_move(self, g):
        c, r = g.tile
        opts = self.maze.options(c, r)
        rev = OPPOSITE[NAME_OF[g.dir]] if g.dir != (0, 0) else None
        legal = [o for o in opts if o != rev] or opts
        if len(legal) == 1:
            g.source = "forced"
            return legal[0]
        if self.nimble_enabled:
            ans = self.nimble.get(g.name)
            if ans and ans["choice"] in legal:
                g.source = "nimble"
                return ans["choice"]
        g.source = "rules"
        return self.rule_move(g, legal)

    # ---- Pac-Man autopilot ----------------------------------------------
    def auto_move(self):
        """Pac-Man decision at a tile centre: Nimble (safety-checked) or rule-based."""
        c, r = self.pac.tile
        opts = self.maze.options(c, r)
        threats = [g.tile for g in self.ghosts.values()
                   if g.mode in ("normal", "leaving") and not g.frightened]
        gd = self.maze.dist_map(threats, limit=8) if threats else {}

        def nxt(d):
            return (c + DIRS[d][0]) % COLS, r + DIRS[d][1]

        def danger(d):               # tiles between the next tile and the nearest threat
            return gd.get(nxt(d), 99)

        if self.pac_brain == "nimble" and self.nimble_enabled:
            ans = self.nimble.get("pacman")
            if ans and ans["choice"] in opts and danger(ans["choice"]) >= 3:
                self.pac_source = "nimble"
                return DIRS[ans["choice"]]

        self.pac_source = "rules"
        avoid = lambda t: gd.get(t, 99) <= 2
        d = None
        if self.fright_until - self.t > 2:                # hunt close frightened ghosts
            prey = {g.tile for g in self.ghosts.values()
                    if g.frightened and g.mode in ("normal", "leaving")}
            if prey:
                d = self.maze.path_first_move((c, r), prey, avoid)
                if d and gd.get(nxt(d), 99) <= 2:
                    d = None
        if d is None:
            d = self.maze.path_first_move((c, r), self.maze.pellets | self.maze.power, avoid)
        if d is None and opts:                            # cornered: run to the safest tile
            d = max(sorted(opts, key=TIE_ORDER.index), key=danger)
        return DIRS[d] if d else (0, 0)

    # ---- simulation -----------------------------------------------------
    def update(self):
        if self.state != "playing":
            return
        if self.dying:
            self.dying -= 1
            if self.dying == 0:
                if self.lives <= 0:
                    self.state = "gameover"
                else:
                    self.reset_positions()
            return
        if self.ready:
            if not self.waiting_for_nimble:
                self.ready -= 1
            return
        self.t += 1 / FPS

        for g in self.ghosts.values():
            if g.mode == "house" and self.t >= g.release_at:
                g.mode = "leaving"
        if self.fright_until and self.t >= self.fright_until:
            self.fright_until = 0.0
            for g in self.ghosts.values():
                g.frightened = False

        self.pac.update(self.maze, self.pac_wanted, self.auto_move if self.autopilot else None)
        self.eat_pellets()
        for g in self.ghosts.values():
            g.update(self)
        self.handle_collisions()

        if not self.maze.pellets and not self.maze.power:
            self.state = "won"

    def eat_pellets(self):
        c, r = self.pac.tile
        cx, cy = c * T + T // 2, r * T + T // 2
        if abs(self.pac.x - cx) + abs(self.pac.y - cy) > 8:
            return
        if (c, r) in self.maze.pellets:
            self.maze.pellets.remove((c, r))
            self.score += 10
        elif (c, r) in self.maze.power:
            self.maze.power.remove((c, r))
            self.score += 50
            self.fright_until = self.t + FRIGHT_SECONDS
            self.combo = 0
            for g in self.ghosts.values():
                if g.mode in ("normal", "leaving"):
                    if g.mode == "normal" and g.dir != (0, 0):
                        g.dir = (-g.dir[0], -g.dir[1])      # ghosts reverse on power pellet
                    g.frightened = True

    def handle_collisions(self):
        for g in self.ghosts.values():
            if g.mode in ("house", "eaten"):
                continue
            if math.hypot(g.x - self.pac.x, g.y - self.pac.y) < T * 0.6:
                if g.frightened:
                    g.mode, g.frightened = "eaten", False
                    self.score += 200 * (2 ** self.combo)
                    self.combo += 1
                else:
                    self.lives -= 1
                    self.dying = 70
                    return

    def reset_positions(self):
        self.pac.reset()
        for g in self.ghosts.values():
            g.reset(self.t)
        self.fright_until = 0.0
        self.pac_wanted = (0, 0)
        self.ready = 90

    # ---- rendering ------------------------------------------------------
    def _render_walls(self):
        surf = pygame.Surface((COLS * T, ROWS * T))
        surf.fill(BLACK)
        for r in range(ROWS):
            for c in range(COLS):
                ch = MAZE[r][c]
                x, y = c * T, r * T
                if ch == "#":
                    pygame.draw.rect(surf, WALL_FILL, (x, y, T, T))
                    for d, (dx, dy) in DIRS.items():
                        nc, nr = c + dx, r + dy
                        if 0 <= nr < ROWS and MAZE[nr][nc % COLS] != "#":
                            if d == "left":
                                pygame.draw.line(surf, WALL_EDGE, (x, y), (x, y + T - 1), 3)
                            elif d == "right":
                                pygame.draw.line(surf, WALL_EDGE, (x + T - 1, y), (x + T - 1, y + T - 1), 3)
                            elif d == "up":
                                pygame.draw.line(surf, WALL_EDGE, (x, y), (x + T - 1, y), 3)
                            else:
                                pygame.draw.line(surf, WALL_EDGE, (x, y + T - 1), (x + T - 1, y + T - 1), 3)
                elif ch == "-":
                    pygame.draw.line(surf, (255, 150, 255), (x, y + T // 2), (x + T, y + T // 2), 3)
        return surf

    def draw_pacman(self, dying=0):
        cx, cy = self.pac.x, self.pac.y + TOP
        ang = math.atan2(self.pac.facing[1], self.pac.facing[0])
        if dying:
            half = math.pi * (1 - dying / 70)                 # mouth opens until gone
        else:
            half = 0.05 + 0.45 * abs(math.sin(self.pac.mouth * 0.25))
        if half >= math.pi - 0.05:
            return
        pts = [(cx, cy)]
        steps = 20
        for i in range(steps + 1):
            a = ang + half + (2 * math.pi - 2 * half) * i / steps
            pts.append((cx + 10 * math.cos(a), cy + 10 * math.sin(a)))
        pygame.draw.polygon(screen, PAC_YELLOW, pts)

    def draw_ghost(self, g):
        x, y = g.x, g.y + TOP
        eyes_only = g.mode == "eaten"
        if not eyes_only:
            if g.frightened:
                left = self.fright_until - self.t
                blink = left < 2 and int(self.t * 6) % 2 == 0
                col = WHITE if blink else FRIGHT_BLUE
            else:
                col = g.color
            pygame.draw.circle(screen, col, (x, y - 2), 10)
            pygame.draw.rect(screen, col, (x - 10, y - 2, 20, 9))
            for fx in (-6, 0, 6):
                pygame.draw.circle(screen, col, (x + fx, y + 7), 4)
        if g.frightened and not eyes_only:
            for ex in (-4, 4):
                pygame.draw.circle(screen, (255, 200, 200), (x + ex, y - 3), 2)
        else:
            for ex in (-4, 4):
                pygame.draw.circle(screen, WHITE, (x + ex, y - 3), 4)
                pygame.draw.circle(screen, (20, 20, 200), (x + ex + g.dir[0] * 2, y - 3 + g.dir[1] * 2), 2)

    def draw_overlay(self):
        labels = [("PAC", self.pac)] + [(n[:3].upper(), g) for n, g in self.ghosts.items()]
        for label, a in labels:
            t = FONT_S.render(f"{label}{a.tile}", True, WHITE)
            screen.blit(t, (a.x - t.get_width() // 2, a.y + TOP - 26))
        for n, g in self.ghosts.items():
            if g.mode == "normal" and not g.frightened:
                tx, ty = self.target_tile(g)
                px, py = tx * T + T // 2, ty * T + T // 2 + TOP
                pygame.draw.line(screen, g.color, (g.x, g.y + TOP), (px, py), 1)
                pygame.draw.rect(screen, g.color, (px - 4, py - 4, 8, 8), 1)

    def draw_panel(self):
        x0, y = COLS * T + 14, TOP + 6
        pygame.draw.line(screen, WALL_EDGE, (COLS * T + 4, TOP), (COLS * T + 4, HEIGHT), 2)

        def line(txt, col=WHITE, font=FONT, dy=20):
            nonlocal y
            screen.blit(font.render(txt, True, col), (x0, y))
            y += dy

        n = self.nimble
        if not self.nimble_enabled:
            line("NIMBLE: OFF  (rules)", (255, 200, 80), FONT_B)
        elif n.online:
            line(f"NIMBLE: ONLINE {n.latency_ms}ms", (90, 255, 120), FONT_B)
        else:
            line("NIMBLE: OFFLINE -> rules", (255, 90, 90), FONT_B)
        line(f"model {NIMBLE_MODEL}", (150, 150, 150), FONT_S, 22)

        line("PAC-MAN", PAC_YELLOW, FONT_B)
        line(f" tile  ({self.pac.tile[0]:>2},{self.pac.tile[1]:>2})")
        line(f" pixel ({self.pac.x:>3},{self.pac.y + TOP:>3})")
        line(f" dir   {NAME_OF.get(self.pac.dir, 'stop')}")
        if self.autopilot:
            lat = n.latest("pacman") if self.pac_brain == "nimble" and self.nimble_enabled else None
            line(f" AUTO brain:{self.pac_brain}")
            line(f" nimble {lat['choice'] if lat else '-':<6} used:{self.pac_source}", dy=26)
        else:
            line(" brain HUMAN", dy=26)

        for name, g in self.ghosts.items():
            line(name.upper(), g.color, FONT_B)
            line(f" tile  ({g.tile[0]:>2},{g.tile[1]:>2})  {g.state_name}")
            lat = n.latest(name)
            if lat:
                p = lat["probs"].get(lat["choice"])
                ptxt = f" {p:.2f}" if isinstance(p, (int, float)) else ""
                nim = f"{lat['choice']}{ptxt}"
            else:
                nim = "-"
            line(f" nimble {nim:<12} used:{g.source}", dy=26)

    def draw(self):
        screen.fill(BLACK)
        screen.blit(self.wall_surface, (0, TOP))
        for (c, r) in self.maze.pellets:
            pygame.draw.circle(screen, PELLET, (c * T + T // 2, r * T + T // 2 + TOP), 2)
        if int(time.time() * 3) % 2 == 0:
            for (c, r) in self.maze.power:
                pygame.draw.circle(screen, PELLET, (c * T + T // 2, r * T + T // 2 + TOP), 7)

        if self.state == "playing" and not self.dying:
            self.draw_pacman()
        elif self.dying:
            self.draw_pacman(self.dying)
        if not self.dying:
            for g in self.ghosts.values():
                self.draw_ghost(g)
        if self.overlay:
            self.draw_overlay()

        hud = f"SCORE {self.score:06d}   LIVES {'o ' * max(self.lives, 0)}  PELLETS {len(self.maze.pellets) + len(self.maze.power)}"
        hud += "   [AUTO]" if self.autopilot else ""
        screen.blit(FONT_B.render(hud, True, WHITE), (10, 12))
        self.draw_panel()
        keys_hint = "Arrows/WASD | N nimble | A auto | C coords | R"
        screen.blit(FONT_S.render(keys_hint, True, (140, 140, 140)), (COLS * T + 14, HEIGHT - 20))

        if self.ready and self.state == "playing" and not self.dying:
            msg = "WAITING FOR NIMBLE...  (N = play without)" if self.waiting_for_nimble else "READY!"
            t = FONT_B.render(msg, True, PAC_YELLOW)
            screen.blit(t, t.get_rect(center=(COLS * T // 2, 11 * T + TOP + 12)))
        if self.state in ("won", "gameover"):
            msg, col = ("YOU WIN!", PAC_YELLOW) if self.state == "won" else ("GAME OVER", (255, 70, 70))
            ov = pygame.Surface((COLS * T, ROWS * T), pygame.SRCALPHA)
            ov.fill((0, 0, 0, 160))
            screen.blit(ov, (0, TOP))
            t = FONT_BIG.render(msg, True, col)
            s = FONT.render(f"Score {self.score} - press R to play again", True, WHITE)
            screen.blit(t, t.get_rect(center=(COLS * T // 2, HEIGHT // 2 - 10)))
            screen.blit(s, s.get_rect(center=(COLS * T // 2, HEIGHT // 2 + 30)))


KEY_DIRS = {
    pygame.K_LEFT: "left", pygame.K_a: "left",
    pygame.K_RIGHT: "right", pygame.K_d: "right",
    pygame.K_UP: "up", pygame.K_w: "up",
    pygame.K_DOWN: "down", pygame.K_s: "down",
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Pac-Man with Ollama Nimble ghosts (and an optional Nimble autopilot).",
        epilog="in-game keys: arrows/WASD move, A autopilot, N nimble on/off, C coords, R restart",
    )
    p.add_argument("--mode", choices=["nimble", "auto", "rules"], default="nimble",
                   help="nimble: you play, Nimble ghosts | auto: Pac-Man plays itself | "
                        "rules: you play, rule-based ghosts (no Ollama)")
    p.add_argument("--pac-brain", choices=["nimble", "rules"], default="nimble",
                   help="who steers Pac-Man in auto mode (default: nimble, falls back to rules)")
    p.add_argument("--model", default=NIMBLE_MODEL, help="Ollama model name")
    p.add_argument("--url", default=OLLAMA_SYSTEMONE, help="/v1/systemone endpoint")
    p.add_argument("--interval", type=float, default=DECISION_INTERVAL_S,
                   help="min seconds between Nimble requests")
    p.add_argument("--stale", type=float, default=STALE_AFTER_S,
                   help="ignore Nimble answers older than this many seconds")
    p.add_argument("--timeout", type=float, default=REQUEST_TIMEOUT_S, help="request timeout (s)")
    p.add_argument("--lives", type=int, default=START_LIVES)
    p.add_argument("--print-state", action="store_true",
                   help="print every state JSON (tile + pixel coordinates) sent to Nimble")
    return p.parse_args(argv)


def main(argv=None):
    global OLLAMA_SYSTEMONE, NIMBLE_MODEL, DECISION_INTERVAL_S, STALE_AFTER_S, REQUEST_TIMEOUT_S, START_LIVES
    args = parse_args(argv)
    OLLAMA_SYSTEMONE, NIMBLE_MODEL = args.url, args.model
    DECISION_INTERVAL_S, STALE_AFTER_S = args.interval, args.stale
    REQUEST_TIMEOUT_S, START_LIVES = args.timeout, args.lives

    init_display()
    game = Game(mode=args.mode, pac_brain=args.pac_brain, print_state=args.print_state)
    clock = pygame.time.Clock()
    end_frames = 0
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                pygame.quit()
                sys.exit()
            if event.type == pygame.KEYDOWN:
                if event.key in KEY_DIRS:
                    game.pac_wanted = DIRS[KEY_DIRS[event.key]]   # buffered turn
                    if game.autopilot:                            # human takes over
                        game.autopilot, game.pac_source = False, "human"
                elif event.key == pygame.K_a:
                    game.autopilot = not game.autopilot
                    game.pac_wanted = game.pac.dir
                    game.pac_source = "human" if not game.autopilot else game.pac_source
                elif event.key == pygame.K_r:
                    game.restart()
                elif event.key == pygame.K_n:
                    game.nimble_enabled = not game.nimble_enabled
                    if game.nimble_enabled:
                        game.nimble.start()
                elif event.key == pygame.K_c:
                    game.overlay = not game.overlay
        game.update()
        game.draw()
        pygame.display.flip()
        clock.tick(FPS)
        # unattended demo: restart a few seconds after win / game over
        end_frames = end_frames + 1 if (game.autopilot and game.state != "playing") else 0
        if end_frames > FPS * 3:
            game.restart()
            end_frames = 0


if __name__ == "__main__":
    main()