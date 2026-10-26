// Incremental whole-file SHA-256: bounded memory even for 25 GB+ files.
importScripts("/static/sha256.min.js");
self.onmessage = async ({ data: file }) => {
  try {
    const hash = self.sha256.create();
    const blockSize = 4 * 1024 * 1024;
    for (let offset = 0; offset < file.size; offset += blockSize) {
      hash.update(new Uint8Array(await file.slice(offset, offset + blockSize).arrayBuffer()));
    }
    self.postMessage({ type: "done", sha256: hash.hex() });
  } catch (error) { self.postMessage({ type: "error", error: String(error) }); }
};
