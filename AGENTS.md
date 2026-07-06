# AISubtitle Agent Notes

- AISubtitle 要固定部署到 `/Applications/AISubtitle.app` 後再啟動，不要每次從 `dist/AISubtitle.app` 開。固定路徑可以減少 macOS TCC / Screen Recording / Accessibility 權限反覆變動。
- redeploy 時使用 `scripts/deploy-app.sh`，裡面用 `ditto dist/AISubtitle.app /Applications/AISubtitle.app` 做 in-place update，然後用穩定 code signing identity 重新簽 `/Applications/AISubtitle.app`。不要 `rm -rf /Applications/AISubtitle.app` 後重建，這會提高 TCC 重新認 app 的機率。
- 如果 relaunch 後 TCC 又變，先查 `codesign -dv --verbose=4 /Applications/AISubtitle.app`。正常應該要看到穩定的 Apple Development/TeamIdentifier，不應該是 `Signature=adhoc` 或 `Identifier=aisubtitle-...`。
- 如果要重啟目前的字幕 app，先停 `/Applications/AISubtitle.app/Contents/MacOS/AISubtitle` 和它底下的 `qwen3-asr-stdin`、`codex-translate-lines.sh`，再用 `open -n /Applications/AISubtitle.app` 啟動。
