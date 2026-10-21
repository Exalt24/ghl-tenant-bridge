// Worker entry. A Workers module may export only handlers, so everything else lives in core.ts.
import { createWorker } from "./core.ts";

export default createWorker();
