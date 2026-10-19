# Final Cut Pro 快捷鍵對照表

Video Console 的「設定 › 快捷鍵」有兩組預設：「Video Console」與「Final Cut Pro」。下表列出 Final Cut Pro 剪輯常用鍵，以及在 Video Console 對應的指令與兩組預設的鍵位。

來源（2026-10-18 查閱）：

- Apple 支援，Keyboard shortcuts in Final Cut Pro for Mac：https://support.apple.com/guide/final-cut-pro/keyboard-shortcuts-ver90ba5929/mac
- 台灣區網址（內容同為英文）：https://support.apple.com/zh-tw/guide/final-cut-pro/ver90ba5929/mac

Mac 對 Windows：Command 對應 Ctrl，Option 對應 Alt。Video Console 在 Mac 上按 Command 也當成 Ctrl。

## 播放與移動

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| Play/Pause | Space | 播放或暫停 | Space | Space | |
| Play Reverse | J、Shift-Space | 倒著播放 | J | J、Shift+Space | 只有正常速度，連按不加速；倒播是逐格畫面，沒有聲音 |
| Stop | K | 停止 | K | K | |
| Play Forward | L | 往前播放 | L | L | 只有正常速度，連按不加速 |
| Go to Previous Frame | ← | 前一格 | ← | ← | |
| Go to Next Frame | → | 後一格 | → | → | |
| Go Back 10 Frames | Shift-← | 倒退 10 格 | 無 | Shift+← | 「Video Console」組的 Shift+← 是倒退 1 秒 |
| Go Forward 10 Frames | Shift-→ | 前進 10 格 | 無 | Shift+→ | 「Video Console」組的 Shift+→ 是前進 1 秒 |
| Go to Previous Edit | ; | 上一個鏡頭（剪接點） | ↑ | ↑、; | ↑ 是 Video Console 原有的鍵，兩組都保留 |
| Go to Next Edit | ' | 下一個鏡頭（剪接點） | ↓ | ↓、' | ↓ 同上 |
| Go to Beginning | Home | 跳到開頭 | Home | Home | |
| Go to End | End | 跳到結尾 | End | End | |
| Play Around | Shift-? | 無 | 無 | 無 | 未實作；? 在 Video Console 是「快捷鍵一覽」 |

## 剪輯與時間軸

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| Blade（在播放頭切開） | Command-B | 在播放頭分割片段 | S | Ctrl+B | 有選取時分割選取的軌道（上方影片、音樂、配音、字幕），沒有選取或選取鏡頭時分割畫面軌 |
| Trim Start | Option-[ | 向左裁剪（刪掉播放頭前面的部分） | Q | Alt+[ | 作用的軌道同分割；畫面軌後面的鏡頭會往前補，其他軌只移動片段起點。Mac 上 Option+[ 可能無法觸發，見 GAPS.md 第 129 條 |
| Trim End | Option-] | 向右裁剪（刪掉播放頭後面的部分） | W | Alt+] | 作用的軌道同分割，畫面軌後面的鏡頭會往前補，其他軌只移動片段終點；沒有選取時 Final Cut Pro 裁剪最上層的片段，Video Console 裁剪畫面軌 |
| Add Caption | Option-C | 在播放頭新增字幕 | Alt+C | Alt+C | 新增一句 2 秒的字幕 |
| Zoom In | Command-加號（+） | 放大時間軸 | =、+ | Ctrl+= | |
| Zoom Out | Command-減號（-） | 縮小時間軸 | - | Ctrl+- | |
| Zoom to Fit | Shift-Z | 時間軸符合視窗寬度 | Shift+Z | Shift+Z | |
| Snapping | N | 開關磁吸對齊 | N | N | |
| Delete | Delete | 刪除選取的片段 | Delete、Backspace | Delete、Backspace | 刪除鏡頭時會先跳確認框 |
| Undo Change | Command-Z | 復原 | Ctrl+Z | Ctrl+Z | 全部頁面 |
| Redo Change | Shift-Command-Z | 重做 | Ctrl+Shift+Z、Ctrl+Y | Ctrl+Shift+Z、Ctrl+Y | 全部頁面 |
| Select All | Command-A | 全選 | Ctrl+A | Ctrl+A | 只在分鏡頁；剪輯頁沒有全選 |
| Duplicate | Command-D | 複製選取的鏡頭 | Ctrl+D | Ctrl+D | 只在分鏡頁 |
| Set Range Start／End | I／O | 無 | 無 | 無 | 未實作；Video Console 沒有範圍選取 |
| Blade 工具 | B | 無 | 無 | 無 | 未實作；時間軸沒有工具模式，請用「在播放頭分割片段」 |
| Select 工具 | A | 無 | 無 | 無 | 未實作，原因同上 |

## 自訂與限制

- 每個指令可以改鍵、新增多組鍵、移除鍵；自訂過的指令標示「已自訂」，可以單項還原或全部還原。自訂過的指令不受預設組切換影響。
- 同一頁的兩個指令用到同一組鍵時，兩邊都會顯示警告，實際只有其中一個會作用。
- 設定存在 settings.json 的 ui.keymap，全 App 共用，重新整理或重開都會保留。
- 由瀏覽器保留、網頁攔不到的鍵，錄製時會擋下並提示換一組：Ctrl+T、Ctrl+W、Ctrl+N、Ctrl+Shift+T、Ctrl+Shift+N、Ctrl+Shift+W、Ctrl+Tab、Ctrl+Shift+Tab、Alt+F4。
- 錄製時 Esc 用來取消，所以不能把 Esc 錄成新鍵；內建的 Esc 鍵位不受影響。
- 落差與未驗證項目見專案根目錄 GAPS.md 的「快捷鍵」一節。
