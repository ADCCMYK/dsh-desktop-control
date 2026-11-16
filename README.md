# dsh-desktop-control

把 Windows 桌面变成一组 AI 可调用的工具：截图、窗口枚举与聚焦、键鼠注入、序列宏、后台观察。动作与截图全部在 Python 侧完成，Node 只负责插件壳与工具注册。

安装后默认关闭，不注册任何工具，去设置里打开开关才启用。

这不是 DeepSeek 官方插件，是第三方社区插件，与 DeepSeek 官方无关，也不包含在任何官方发行版

## 安装

安装与卸载由 DSH 的 `plugin_manager` 完成，不需要手工修改 profile 的 `package.json`。支持两种来源。

### 在线：从 Git 仓库安装

```bash
plugin_manager install_bundle target=github:<你的GitHub账号>/<仓库名>
plugin_manager install_bundle target=github:<你的GitHub账号>/<仓库名>#main
```

第二行是指定分支的写法，也可以换成分支名、标签或提交号。

也支持完整的 Git 地址或仓库网页地址：

```bash
plugin_manager install_bundle target=https://github.com/<你的GitHub账号>/<仓库名>
```

### 离线：本地目录或 tgz

离线包可以是仓库目录，也可以是打包好的 tgz，都不需要构建。

1. 仓库目录

   ```bash
   plugin_manager install_bundle target=D:\path\to\dsh-desktop-control
   ```

2. tgz 包

   ```bash
   plugin_manager install_bundle target=D:\path\to\dsh-desktop-control-<版本号>.tgz
   ```

安装后插件默认关闭，不会注册任何桌面工具，去设置里打开开关才启用（见下面「首次使用」）。

如果启用失败，插件会注册 `desktop_control_diag`，调用它会回失败原因与修法。

## 环境要求

- Windows 10/11
- Python 3.10+（3.11、3.13 实测）
- `python` 要能在命令行直接运行，不在 PATH 时见下面「配置」
- Python 依赖：mss、Pillow、pywin32、psutil、numpy

### 安装 Python 依赖

在插件目录下执行（`requirements.txt` 就在插件根目录）：

```bash
python -m pip install -r requirements.txt
```

装到你的 Python 环境即可，不需要管理员权限。装完可以验证：

```bash
python -c "import mss, PIL, psutil, numpy, win32api; print('ok')"
```

如果 `python` 不是你想用的那个解释器，把命令里的 `python` 换成完整路径。

## 工具

共 17 个：

- `capture_screenshot`：截图，支持全屏、窗口、显示器和裁剪，默认直接返回图片，也可指定 `save_path` 落盘
- `list_windows`：列窗口，默认过滤不可见窗口
- `focus_window`：聚焦窗口
- `set_window`：调整窗口位置与大小
- `get_mouse_position`：读光标位置
- `mouse_move`：移动光标，支持人手化轨迹与相对位移
- `mouse_click`：点击，支持就地点击与生效判定
- `mouse_drag`：拖拽
- `mouse_transfer`：搬移
- `hover`：悬停
- `scroll`：滚轮
- `send_keystrokes`：输入文本
- `send_shortcut`：发组合键
- `open_desktop_item`：打开桌面或开始菜单项
- `wait`：等待，支持条件监听
- `find_click`：模板匹配找图、按图点击或搬移
- `sequence`：键鼠序列与宏

## 感知方式与取舍

这个插件把 UIA（控件树）、OCR、图标识别整层删掉了，感知只剩截图：找窗口、找按钮、判断结果，全靠把画面交给模型自己看。

代价是 token。每看一眼就是一张图，定位越勤、截图越多，消耗越大，长会话、连续操作时尤其明显。

代价还有误差。坐标是从图里目测算出来的，不是控件回报的，可能差几个像素，分辨率缩放、窗口边框、页面滚动都会让这点误差变大。所以关键动作别指望一次点中：看回执的生效判定，拿不准就先截一张局部图核对。

换来的是位置自由。不依赖控件树、不依赖文字识别、不依赖图标模型，所以屏幕上看得到的任意位置都能操作：游戏画面、画布、自绘界面、远程桌面、非标准控件，凡是识别层认不出来的东西，一律照点不误。只要能在截图里指出它在哪，就能点过去。

省 token 的做法：

- 能一次整图看完，就别反复截
- 要看细节再传 `rect` 裁局部
- 后台观察用 `push_interval_ms` 节流
- 只想要文字结果、不要图时传 `include_image=false`

## 首次使用

1. 打开 设置 → 桌面控制
2. 打开「启用桌面控制」开关
3. 回到对话，17 个桌面工具才会出现，关闭开关会立即注销这些工具

如果打开开关失败，页面会显示原因。缺依赖时会给出三行提示：

```text
缺少 Python 依赖：mss（pip 包：mss）
安装依赖：
  python -m pip install -r "<插件安装目录>\requirements.txt"
```

照着执行后重新拨一次开关即可。

## 配置

`cordis.patch.yml` 的默认配置：

```yaml
config:
  pythonPath: python
```

解释器解析顺序：插件配置 `pythonPath` → 环境变量 `DSH_DESKTOP_PYTHON` → `python`。

如果 `python` 不在 PATH，把 `pythonPath` 改成解释器绝对路径，或设置 `DSH_DESKTOP_PYTHON`。

## 使用说明

- 截图默认随调用返回，只是要看动作后的画面时，不要再单独截一次
- 不传 `save_path` 时图片不落盘
- 后台观察由 `vision_action` 控制，注入节流 `push_interval_ms`（默认 10 秒），容量旋钮 `hero_history`、`sheet_frames`
- 设置页的「附图显示真实光标」：开着时附图叠加系统光标图标并报鼠标位置，关掉后两样都不出，改为提示以应用自己的光标为准。应用自带光标时推荐关闭
- 序列与宏有真实执行能力，运行前确认当前前台窗口与光标位置

## 已知限制

- 全屏窗口自动改走屏幕抓取。PrintWindow 对 D3D/Unity 全屏会反复返回同一张旧帧（实测连抓两次一致），所以检测到窗口铺满整块屏幕时改用屏幕抓取，代价是被遮挡的全屏窗口抓不到自己的画面，只能看到最前面那张
- 独占全屏的游戏失焦后会自己最小化。这时窗口图只有一条标题条，回执会直接说明「窗口已最小化，要看内容需要先切到前台」。想边看边操作，建议让游戏跑无边框窗口化
- 相对位移在 3D 游戏视角下没有实测。`relative=true` 的增量语义只在桌面和 2D 应用上验过，3D 里视角转多少、会不会被系统鼠标加速放大，没有实机数据，用之前先小步试
- 生效判定依赖取帧时机。全屏游戏画面变化很快，动作刚放开就采样时可能还没开始变，回执会写「未检出变化」并提示结论仅供参考

## 常见问题

- 打开开关提示缺少 Python 依赖：按提示执行 pip 命令。一次只会报第一个缺的包，装完再拨一次开关
- 提示找不到 `python`：确认 `python --version` 能运行，不能运行就装 Python 并勾选 Add to PATH，或在 `cordis.patch.yml` 把 `pythonPath` 改成解释器绝对路径，或设置环境变量 `DSH_DESKTOP_PYTHON`
- 开关打开了但工具没出现：刷新一次页面再看，仍没有就调用 `desktop_control_diag` 查原因
- 点了没反应：先看回执的生效判定，再截一张局部图核对坐标，坐标是目测的，可能有几像素误差
- 卸载：`plugin_manager remove_bundle target=dsh-desktop-control`，不需要手工删文件

## 免责声明

本插件通过模拟键盘鼠标和屏幕截图操作本机桌面，具有真实执行能力。使用即表示你理解并同意：

- 仅在你拥有或已获授权控制的设备上使用，不得用于未经授权的访问、控制或规避安全机制
- 模拟输入可能造成误点击、误输入、数据修改或丢失，运行序列或宏前请关闭重要窗口并备份数据
- 截图可能包含敏感信息，截图会作为工具结果返回给调用它的 AI 客户端，是否发送、发送给谁由你使用的客户端及其配置决定
- 请自行确认使用方式符合当地法律法规以及第三方软件许可条款
- 本软件按「现状」提供，不提供任何明示或暗示的担保，本项目的维护者与贡献者不对使用本软件造成的任何直接或间接损失负责

## 许可

MIT，见 [LICENSE](LICENSE)。
