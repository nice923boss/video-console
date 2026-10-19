// Display names for workflow template kinds, modes and the binding keys the app fills in itself.
export const KIND_TEXT = { image: "圖片", video: "影片", upscale: "放大" };
export const MODE_TEXT = { t2i: "文字生圖", i2i: "參考圖生圖", i2v: "圖轉影片", t2v: "文字生影片", upscale: "放大" };
export const MODES_BY_KIND = { image: ["t2i", "i2i"], video: ["i2v", "t2v"], upscale: ["upscale"] };
export const BINDING_TEXT = {
  prompt: "提示詞", negative: "負向提示詞", seed: "種子", width: "寬度", height: "高度", frames: "影格數",
  fps: "影格率", image: "輸入圖片", prefix: "輸出檔名", denoise: "重繪強度",
};
