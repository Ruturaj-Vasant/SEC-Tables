/**
 * `bridge.py` is bundled as text (esbuild's `text` loader), so the Python the
 * worker installs is part of the worker bundle rather than a second fetch.
 */
declare module "*.py" {
  const source: string;
  export default source;
}
