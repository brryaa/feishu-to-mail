# Feishu File Mailer

Windows 10 下运行的 Python 程序。

功能：

- 通过飞书开放平台长连接接收机器人消息事件
- 仅处理文件消息
- 将收到的文件下载到本地指定目录
- 调用 Windows Shell 上文件的 `Foxmail` 动作，把文件作为附件打开写信窗口
- 自动填写固定收件人，并发送 `Ctrl+Enter`

## 依赖

建议在 Windows 机器上安装：

- Python 3.10+
- `lark-oapi`
- `requests`
- `pywin32`
- `pywinauto`

安装示例：

```bat
pip install -r requirements.txt
```

## 飞书侧配置

需要在飞书开放平台的自建应用中完成以下配置：

1. 开启机器人能力
2. 订阅事件：`im.message.receive_v1`
3. 事件订阅方式选择：`使用长连接接收事件`
4. 申请至少一项消息接收权限：
   - 单聊：`im:message.p2p_msg:readonly`
   - 群聊 @ 机器人：`im:message.group_at_msg:readonly`
   - 或你实际需要的群消息权限

## 本地配置

复制 `config.example.json` 为 `config.json`，填写：

- `app_id`
- `app_secret`
- `download_dir`
- `recipient_email`
- `foxmail_window_title_regex`

如果你的 Windows 右键发送到菜单里不是 `Foxmail` 这个文字，也可以改：

- `shell_verb_keyword`

Windows 路径注意：

- JSON 里不能直接写 `C:\FeishuDownloads`
- 应写成 `C:\\FeishuDownloads`
- 或更简单，直接写成 `C:/FeishuDownloads`

## 运行

```bat
python main.py --config config.json
```

或直接双击：

- `run.bat`

## 打包 exe

在 Windows 上安装依赖后执行：

```bat
build_windows.bat
```

生成物会在 `dist\FeishuFileMailer\FeishuFileMailer.exe`。

## 说明

1. 飞书事件要求尽快返回，因此程序会把任务放进后台队列处理，避免长时间阻塞事件线程。
2. 为避免重复下载或重复发件，程序会按 `message_id` 做幂等记录，状态保存在 `runtime\state.json`。
3. Foxmail 窗口控件名称在不同版本上可能略有差异，所以自动填写收件人的逻辑做了多重尝试：
   - 优先找顶部可编辑输入框
   - 找不到时退化为键盘输入
4. 如果 Foxmail 首次发送时出现确认框，需要在本机先手工确认一次相关安全提示。
