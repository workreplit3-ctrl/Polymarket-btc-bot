import { timingSafeEqual } from "node:crypto";
import { Router, type IRouter, type Request, type Response } from "express";
import { getBotStatus, setBotPaused } from "../lib/botProcess";

const router: IRouter = Router();

router.get("/bot/status", (_req, res) => {
  res.json(getBotStatus());
});

router.post("/bot/pause", (req, res) => {
  const paused = req.body?.paused;
  updateBotPause(req, res, paused === undefined ? true : paused);
});

router.post("/bot/resume", (req, res) => {
  updateBotPause(req, res, false);
});

function updateBotPause(req: Request, res: Response, paused: unknown) {
  if (!isBotControlAuthorized(req)) {
    res.status(401).json({ error: "Authentication required" });
    return;
  }

  if (typeof paused !== "boolean") {
    res.status(400).json({ error: "paused must be a boolean" });
    return;
  }

  setBotPaused(paused);
  res.json(getBotStatus());
}

function isBotControlAuthorized(req: Request): boolean {
  const expectedSessionSecret = process.env.SESSION_SECRET?.trim();
  const authorization = req.header("authorization");
  const bearerToken = authorization?.match(/^Bearer\s+(.+)$/i)?.[1]?.trim();

  if (expectedSessionSecret && bearerToken) {
    const expected = Buffer.from(expectedSessionSecret);
    const received = Buffer.from(bearerToken);
    if (
      expected.length === received.length &&
      timingSafeEqual(expected, received)
    ) {
      return true;
    }
  }

  const telegramUserId = req.header("x-telegram-user-id")?.trim();
  const allowedUserIds = (process.env.TELEGRAM_USER_ID ?? "")
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean);
  return Boolean(telegramUserId && allowedUserIds.includes(telegramUserId));
}

export default router;