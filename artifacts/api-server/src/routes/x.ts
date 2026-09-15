import { timingSafeEqual } from "node:crypto";
import { Router, type IRouter, type Request, type Response } from "express";
import { fetchXSnapshot } from "../lib/xProxy";
import { logger } from "../lib/logger";

const router: IRouter = Router();

router.get("/internal/x/snapshot", async (req, res) => {
  if (!isBotInternalRequest(req)) {
    res.status(401).json({ error: "Authentication required" });
    return;
  }

  const usernames = String(req.query.usernames ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  const lookbackSeconds = boundedNumber(req.query.lookback_seconds, 60, 1800, 900);
  const maxPosts = boundedNumber(req.query.max_posts, 10, 100, 50);

  try {
    const snapshot = await fetchXSnapshot(usernames, lookbackSeconds, maxPosts);
    res.json(snapshot);
  } catch (error) {
    logger.warn({ err: error }, "X snapshot request failed");
    res.status(502).json({ error: "X snapshot unavailable" });
  }
});

function boundedNumber(
  value: unknown,
  min: number,
  max: number,
  fallback: number,
): number {
  const parsed = Number(value);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, Math.floor(parsed)));
}

function isBotInternalRequest(req: Request): boolean {
  const expected = process.env.SESSION_SECRET?.trim();
  const received = req.header("x-bot-internal-secret")?.trim();
  if (!expected || !received) return false;
  const expectedBuffer = Buffer.from(expected);
  const receivedBuffer = Buffer.from(received);
  return (
    expectedBuffer.length === receivedBuffer.length &&
    timingSafeEqual(expectedBuffer, receivedBuffer)
  );
}

export default router;