import { Router, type IRouter } from "express";
import { getBotStatus } from "../lib/botProcess";

const router: IRouter = Router();

router.get("/bot/status", (_req, res) => {
  res.json(getBotStatus());
});

export default router;