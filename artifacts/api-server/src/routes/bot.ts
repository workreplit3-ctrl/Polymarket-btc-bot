import { Router, type IRouter } from "express";
import { getBotStatus, setBotPaused } from "../lib/botProcess";

const router: IRouter = Router();

router.get("/bot/status", (_req, res) => {
  res.json(getBotStatus());
});

router.post("/bot/pause", (_req, res) => {
  setBotPaused(true);
  res.json(getBotStatus());
});

router.post("/bot/resume", (_req, res) => {
  setBotPaused(false);
  res.json(getBotStatus());
});

export default router;