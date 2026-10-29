# Final Cut Pro 快捷鍵對照表

Video Console 的「設定 › 快捷鍵」有兩組預設：「Video Console」與「Final Cut Pro」。「Video Console」組照剪映的 Final Cut Pro X 方案（時間線分頁）排鍵；「Final Cut Pro」組用 Apple 官方鍵位。下表列出 Final Cut Pro 剪輯常用鍵，以及在 Video Console 對應的指令與兩組預設的鍵位。

來源：

- Apple 支援，Keyboard shortcuts in Final Cut Pro for Mac（2026-10-18 查閱，2026-10-23 再核對標記、音量、區域、定格、分離、變速、複合片段各列）：https://support.apple.com/guide/final-cut-pro/keyboard-shortcuts-ver90ba5929/mac
- 台灣區網址（內容同為英文）：https://support.apple.com/zh-tw/guide/final-cut-pro/ver90ba5929/mac
- Option-K 添加關鍵幀不在上面的總表，出自 Apple 支援 Add video effect keyframes（2026-10-23 查閱）：https://support.apple.com/guide/final-cut-pro/add-video-effect-keyframes-ver8e3f20ea/mac

Mac 對 Windows：Command 對應 Ctrl，Option 對應 Alt。Video Console 在 Mac 上按 Command 也當成 Ctrl。Final Cut Pro 用 Control 鍵的指令（下一個標記、音量、分離音訊、顯示動畫）在 Windows 會和 Command 撞在同一個 Ctrl 上，所以「Final Cut Pro」組不設鍵，需要時自己加。

## 播放與移動

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| Play/Pause | Space | 播放或暫停 | Space | Space | |
| Play Reverse | J、Shift-Space | 倒著播放 | J | J、Shift+Space | 播放中再按一次加速到 2x、4x、8x，暫停後回到 1x；倒播是逐格畫面，沒有聲音 |
| Stop | K | 停止 | 無 | K | 「Video Console」組的 K 是添加關鍵幀 |
| Play Forward | L | 往前播放 | L | L | 播放中再按一次加速到 2x、4x、8x，暫停後回到 1x |
| Go to Previous Frame | ← | 前一格 | ← | ← | |
| Go to Next Frame | → | 後一格 | → | → | |
| Go Back 10 Frames | Shift-← | 倒退 10 格 | 無 | Shift+← | 「Video Console」組的 Shift+← 是倒退 1 秒 |
| Go Forward 10 Frames | Shift-→ | 前進 10 格 | 無 | Shift+→ | 「Video Console」組的 Shift+→ 是前進 1 秒 |
| Go to Previous Edit | ; | 上一個鏡頭（剪接點） | ↑ | ↑、; | ↑ 是 Video Console 原有的鍵，兩組都保留 |
| Go to Next Edit | ' | 下一個鏡頭（剪接點） | ↓ | ↓、' | ↓ 同上 |
| Go to Beginning | Home | 跳到開頭 | Home | Home | |
| Go to End | End | 跳到結尾 | End | End | |
| Play Around | Shift-? | 無 | 無 | 無 | 未實作；? 在 Video Console 是「快捷鍵一覽」 |

## 分割與裁剪

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| Blade | Command-B | 在播放頭分割片段 | Alt+B | Ctrl+B | 有選取時分割選取的軌道（上方影片、音樂、配音、字幕），沒有選取或選取鏡頭時分割畫面軌 |
| Blade All | Shift-Command-B | 在播放頭分割所有軌道 | Alt+Shift+B | Ctrl+Shift+B | 播放頭經過的每一軌一起切開 |
| Trim Start | Option-[ | 向左裁剪（刪掉播放頭前面的部分） | F1 | Alt+[ | 作用的軌道同分割；畫面軌後面的鏡頭會往前補，其他軌只移動片段起點。Mac 上 Option+[ 可能無法觸發，見 GAPS.md 第 129 條 |
| Trim End | Option-] | 向右裁剪（刪掉播放頭後面的部分） | F3 | Alt+] | 作用的軌道同分割；沒有選取時 Final Cut Pro 裁剪最上層的片段，Video Console 裁剪畫面軌 |
| Select 工具 | A | 選擇模式 | A | A | |
| Blade 工具 | B | 分割模式 | B | B | 點片段就在點的位置切開；切回選擇模式才能拖曳與選取 |

## 片段、字幕與標記

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| Enable/Disable | V | 啟用或停用選取的片段 | V | V | 停用的鏡頭保留位置，預覽與輸出是黑畫面加靜音 |
| Add Caption | Option-C | 在播放頭新增字幕 | Alt+C | Alt+C | 新增一句 2 秒的字幕 |
| Add Marker | M | 在播放頭新增標記 | M | M | |
| Add Marker and Modify | Option-M | 切換播放頭處標記的顏色 | Alt+M | 無 | Final Cut Pro 是新增並開編輯視窗；Video Console 沒有標記編輯視窗，顏色與刪除在標記的右鍵選單 |
| Next Marker | Control-' | 跳到下一個標記 | F8 | 無 | Control 鍵的指令不設鍵，見文件開頭 |
| Previous Marker | Control-; | 跳到上一個標記 | F7 | 無 | 同上 |
| Raise Volume 1 dB | Control-= | 選取片段音量加 1dB | Ctrl+. | 無 | 只作用在音訊與上方影片；有區域時只調區域內 |
| Lower Volume 1 dB | Control-- | 選取片段音量減 1dB | Ctrl+, | 無 | 同上 |
| Detach Audio | Control-Shift-S | 分離或還原鏡頭的原聲 | Ctrl+Shift+S | 無 | 變速鏡頭的原聲照速度分離；分離期間不能改速度 |

## 區域

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| Set Range Start | I | 設定區域入點 | I | I | |
| Set Range End | O | 設定區域出點 | O | O | |
| Select Clip Range | X | 以選取片段設定區域 | Shift+X | X | 沒有選取時用播放頭下方的鏡頭 |
| Play Selection | / | 播放區域（入點到出點） | Shift+Enter | / | |
| Clear Selected Ranges | Option-X | 清除區域 | Alt+X | Alt+X | |

## 速度、方向與關鍵幀

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| Show/Hide Retime Editor | Command-R | 變速 | Ctrl+R | Ctrl+R | 開右側「速度與方向」，0.25x 到 4x |
| Reverse Clip | 無 | 倒放選取的鏡頭 | Ctrl+Shift+R | 無 | Final Cut Pro 沒有預設鍵；第一次會在背景產生倒放影片 |
| Insert/Connect Freeze Frame | Option-F | 定格 | Alt+Shift+F | Alt+F | 插入 5 秒靜止畫面 |
| 無 | 無 | 曲線變速 | Shift+B | 無 | [Shell] 按下只提示還沒有支援 |
| Add Keyframe | Option-K | 在播放頭添加關鍵幀 | K | Alt+K | 上方影片記位置、大小、不透明度、音量；音訊記音量；調整圖層記調色 |
| Show Video Animation | Control-V | 顯示或隱藏關鍵幀面板 | Ctrl+K | 無 | Video Console 是右側面板，不是時間軸上的動畫編輯器 |

## 組合與複合片段

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| 無 | 無 | 組合 | Alt+G | 無 | [Shell] 按下只提示還沒有支援 |
| New Compound Clip | Option-G | 新建複合片段 | Ctrl+Shift+G | Alt+G | [Shell] 同上 |
| Break Apart Clip Items | Shift-Command-G | 解除複合片段 | Alt+Shift+G | Ctrl+Shift+G | [Shell] 同上 |
| 無 | 無 | 預合成 | Alt+E | 無 | [Shell] 同上 |

## 時間軸檢視與其他

| Final Cut Pro 指令 | Mac 鍵 | Video Console 指令 | 「Video Console」組 | 「Final Cut Pro」組 | 說明 |
|---|---|---|---|---|---|
| Zoom In | Command-加號（+） | 放大時間軸 | F6 | Ctrl+= | 也可用 Alt 或 Ctrl＋滾輪 |
| Zoom Out | Command-減號（-） | 縮小時間軸 | F5 | Ctrl+- | 同上 |
| Zoom to Fit | Shift-Z | 時間軸符合視窗寬度 | Shift+Z | Shift+Z | |
| 無 | 無 | 增高軌道 | +（Shift+=） | 無 | |
| 無 | 無 | 降低軌道 | _（Shift+-） | 無 | |
| Snapping | N | 開關磁吸對齊 | N | N | |
| 無 | 無 | 開關連動上下軌 | Shift+N | Shift+N | |
| Delete | Delete | 刪除選取的片段 | Delete、Backspace、F2 | Delete、Backspace | 刪除鏡頭時會先跳確認框；F2 是 Video Console 自訂 |
| Undo Change | Command-Z | 復原 | Ctrl+Z | Ctrl+Z | 全部頁面 |
| Redo Change | Shift-Command-Z | 重做 | Ctrl+Shift+Z、Ctrl+Y | Ctrl+Shift+Z、Ctrl+Y | 全部頁面 |
| Select All | Command-A | 全選 | Ctrl+A | Ctrl+A | 只在分鏡頁；剪輯頁沒有全選 |
| Duplicate | Command-D | 複製選取的鏡頭 | Ctrl+D | Ctrl+D | 只在分鏡頁 |

滾輪不分組：滾輪上下捲動軌道，Shift＋滾輪左右捲動；軌道沒有超出高度時，滾輪直接左右捲動。

指令面板兩組都是 Ctrl+Shift+P；Ctrl+K 留給關鍵幀面板。

## 自訂與限制

- 每個指令可以改鍵、新增多組鍵、移除鍵；自訂過的指令標示「已自訂」，可以單項還原或全部還原。自訂過的指令不受預設組切換影響。
- 同一頁的兩個指令用到同一組鍵時，兩邊都會顯示警告，實際只有其中一個會作用。
- 設定存在 settings.json 的 ui.keymap，全 App 共用，重新整理或重開都會保留。
- 由瀏覽器保留、網頁攔不到的鍵，錄製時會擋下並提示換一組：Ctrl+T、Ctrl+W、Ctrl+N、Ctrl+Shift+T、Ctrl+Shift+N、Ctrl+Shift+W、Ctrl+Tab、Ctrl+Shift+Tab、Alt+F4。
- F1、F3、F5、F6、F7、Ctrl+R、Ctrl+Shift+R 在一般瀏覽器分頁可能先被瀏覽器接走（說明、尋找下一個、重新整理、網址列、鍵盤瀏覽模式），請在 Video Console 的 APP 視窗使用。
- 錄製時 Esc 用來取消，所以不能把 Esc 錄成新鍵；內建的 Esc 鍵位不受影響。
- 每個剪輯功能的用法，在 APP 內的「剪輯功能說明」（快捷鍵一覽下方按鈕，或指令面板搜尋）。
- 落差與未驗證項目見專案根目錄 GAPS.md 的「快捷鍵」與「剪映 FCPX 快捷鍵」兩節。
