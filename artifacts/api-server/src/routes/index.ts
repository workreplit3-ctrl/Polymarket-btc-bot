import { Router, type IRouter } from "express";
import botRouter from "./bot";
import healthRouter from "./health";

const router: IRouter = Router();

router.use(healthRouter);
router.use(botRouter);

export default router;
