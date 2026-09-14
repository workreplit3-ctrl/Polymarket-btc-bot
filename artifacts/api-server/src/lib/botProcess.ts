import { spawn, type ChildProcess } from "node:child_process";
import { existsSync, mkdirSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import readline from "node:readline";
import { logger } from "./logger";

type BotState = "starting" | "running" | "stopped" | "error";

let child: ChildProcess | undefined;
let state: BotState = "stopped";
let lastError: string | null = null;
let startedAt: string | null = null;
let exitCode: number | null = null;
const pauseFlag = path.resolve(process.cwd(), "polymarket-btc-bot", "data", "paused.flag");

function attachOutput(stream: NodeJS.ReadableStream, level: "info" | "error") {
  const lines = readline.createInterface({ input: stream });
  lines.on("line", (line) => {
    const message = line.trim();
    if (!message) return;
    if (level === "error") {
      logger.error({ component: "telegram-bot", message }, "Telegram bot stderr");
    } else {
      logger.info({ component: "telegram-bot", message }, "Telegram bot");
    }
  });
}

export function startBotProcess(): void {
  if (child) {
    return;
  }

  const botRoot = path.resolve(process.cwd(), "polymarket-btc-bot");
  const workspaceRoot = path.resolve(process.cwd(), "..", "..");
  const bundledPython = path.join(workspaceRoot, ".pythonlibs", "bin", "python");
  const python = process.env.PYTHON_BIN ?? (existsSync(bundledPython) ? bundledPython : "python");
  const launcher = path.join(botRoot, "run_bot.py");
  state = "starting";
  lastError = null;
  exitCode = null;

  const spawned = spawn(python, [launcher], {
    cwd: botRoot,
    env: { ...process.env, PYTHONUNBUFFERED: "1" },
    stdio: ["ignore", "pipe", "pipe"],
  });
  child = spawned;
  startedAt = new Date().toISOString();
  if (spawned.stdout) attachOutput(spawned.stdout, "info");
  if (spawned.stderr) attachOutput(spawned.stderr, "error");

  spawned.on("spawn", () => {
    state = "running";
    logger.info({ component: "telegram-bot" }, "Telegram bot process started");
  });
  spawned.on("error", (error) => {
    state = "error";
    lastError = error.message;
    logger.error({ err: error, component: "telegram-bot" }, "Telegram bot process error");
  });
  spawned.on("exit", (code, signal) => {
    exitCode = code;
    if (signal && state !== "stopped") {
      lastError = `stopped by signal ${signal}`;
    }
    if (code !== 0 && state !== "stopped") {
      state = "error";
    } else {
      state = "stopped";
    }
    child = undefined;
    logger.info(
      { component: "telegram-bot", code, signal },
      "Telegram bot process exited",
    );
  });
}

export function stopBotProcess(): void {
  if (!child) return;
  state = "stopped";
  child.kill("SIGTERM");
  child = undefined;
}

export function setBotPaused(paused: boolean): void {
  mkdirSync(path.dirname(pauseFlag), { recursive: true });
  if (paused) {
    writeFileSync(pauseFlag, "paused\n", "utf8");
  } else if (existsSync(pauseFlag)) {
    unlinkSync(pauseFlag);
  }
}

export function getBotStatus() {
  return {
    name: "10",
    process: state,
    mode: process.env.POLYMARKET_MODE ?? "paper",
    walletConfigured: Boolean(
      process.env.POLYMARKET_PRIVATE_KEY && process.env.POLYMARKET_FUNDER_ADDRESS,
    ),
    paused: existsSync(pauseFlag),
    startedAt,
    exitCode,
    lastError,
  };
}