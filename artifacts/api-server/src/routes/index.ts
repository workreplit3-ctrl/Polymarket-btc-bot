import { Router, type IRouter } from "express";
import botRouter from "./bot";
import healthRouter from "./health";
import xRouter from "./x";

const router: IRouter = Router();

router.use(healthRouter);
router.use(botRouter);
router.use(xRouter);

export default router;
