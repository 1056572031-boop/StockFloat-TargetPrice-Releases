# 连续合成画面取证（工具侧，不进入产品）

目的：消除单张`screencapture`启动迟到与拖动期间的取证盲区。使用系统ScreenCaptureKit录制局部屏幕视频，AVFoundation编码/逐帧解码，不安装ffmpeg或新Python包，不改产品绘制或窗口逻辑。

## 运行

使用工程`.venv`，在有窗口服务、已具备录屏权限的开发Mac上执行；需Command Line Tools编译工具侧Swift助手。助手不进应用包，正式用户的安装/更新不依赖它。

```bash
./.venv/bin/python tools/native_glass_probe.py --continuous-video --exercise --fullscreen --seconds 0.2 --out build/continuous-check-liquid
./.venv/bin/python tools/native_glass_probe.py --continuous-video --exercise --fullscreen --legacy --seconds 0.2 --out build/continuous-check-legacy
```

每次使用新输出目录；不复用旧的`continuous/`。运行时会短暂显示测试悬浮窗并进出测试全屏窗口。配置、日志、首屏缓存、socket均隔离，网络/摄像头关闭；录制没有音频，不主动请求录屏权限。

真实负向对照：

```bash
./.venv/bin/python tools/native_glass_probe.py --continuous-video --continuous-control hide --exercise --fullscreen --seconds 0.2 --out build/continuous-check-hide
```

该模式在拖动阶段故意隐藏测试窗口250ms，预期连续画面判据失败；它不是产品新增缺陷，不登记为线上逃逸。无`--continuous-video`不得使用此开关。

## 数据链与拒绝条件

1. 捕获第一张完整帧、写入就绪证明、核对Python/Swift单调时钟后，才开始构造测试窗口。若首个显示时间指向下一次刷新，等该帧已先于业务触发，不拿未来帧当事前就绪。
2. 源帧保留ScreenCaptureKit状态、mach显示时间、交付时间及PTS。SDK定义idle为“屏幕未变，没有生成新帧”，只有这种明确状态可以沿用上一张已解码像素；无前序帧不得沿用。blank/suspended/未知状态拒绝。
3. 完整源帧必须逐一编码；写入背压、追加失败、缺像素缓冲、异常停流都拒绝。音频与麦克风不采集。
4. AVAssetReader必须读到完整EOF。完整源帧数量与解码帧数量逐一相等，相对PTS误差不得超过2ms；不能拿“视频文件存在”当成功。
5. 固定区域为全局坐标`[360,140,460,360]`、1像素/逻辑点。原生窗口为预置缓存的184×224pt。实际视频逐帧搜索左侧价格数字模板，并要求文字前景与暗背景同时成立；全黑、全白、纯色底不能自证有价格。原生自检还验了彩色BGRA及移动模板位置。
6. 视频文件解码前后哈希须一致，Swift助手/驱动源码运行期间不得变更。视频、源帧、解码帧、事件、配置、模板与助手二进制/源码哈希写入`analysis.json`。

源时间采用`SCStreamFrameInfoDisplayTime`，不是收到截图文件的时刻。语义已核对本机Apple SDK的`ScreenCaptureKit.framework/Headers/SCStream.h`：状态定义与mach绝对显示时刻。未依赖采样者自己猜测idle含义。

## 六个时段，四个验收阶段

事件顺序为startup、fullscreen_transition、fullscreen、desktop_transition、desktop_return、drag。过渡期间也连续录制，不删视频、不删除缺价格计数。

四个目标阶段检查：

下面的数值是独立固定的工具侧验收预算，不由产品常量反推，也不是已对用户承诺的启动性能或统计概率；正式接入发布链前仍需复核适用范围。

- 记录必须覆盖阶段开始之前与结束之后，不能晚开流或提前停流。
- 相邻源样本最大间隔100ms，交付落后显示时刻最大150ms，且至少有15次/秒的源样本密度。
- startup允许从窗口构造到首个有效价格最多500ms；其后的三个阶段首个可见价格最多100ms。
- 首个有效价格出现后，缺价格区间的保守上界按“前一张好帧→后一张好帧”计算，超过100ms拒绝。不能只用两张坏帧相减，导致单帧丢失变0ms。
- 原有真实paintEvent几何/有效价格与WindowServer双层/拖动判据独立保留；连续视频过了，不替它们放行。

两个`*_transition`区间在`unqualified_transitions`中保留。Space动画能把卡片移出固定取景区；这不直接等于应用只剩背景或内容消失。fullscreen/desktop_return必须由系统完成回执与活动Space变化证明后立即开始，**不能先等窗口自愈再开始计时**。当前固定取景区不对动画过程本身给像素可见性合格结论。

`continuous_video.passed`只代表上述四个目标阶段及编码/解码证据链通过；不代表两个动画区间、全系统、全设备或正式安装产物通过。

## 证据与范围

- `continuous/capture.mov`：实际合成视频。
- `continuous/source-frames.json`：完整/idle等源回调、时钟、写入结果。
- `continuous/decoded-frames.json`：每个实际编码帧的解码PTS、价格可见性及位置。
- `continuous/events.json`：六个阶段边界。
- `continuous/frames/`：可见性变化处的代表帧，供复核“移出取景区”与“缺内容”。
- `continuous/analysis.json`：逐阶段计数、时长上界、失败原因与哈希。

阶段`frames`/`observed_bad_frames`计的是源回调样本，包含系统明确的idle；`decoded_frames`才是实际编码后解码的图像帧数。不能把idle样本当成额外独立图像或额外检测样本。

未观察到坏样本不等于零空窗：必须同时报告最大源样本间隔，100ms以内的未采到变化不在证明范围内。价格像素位置不是玻璃边缘像素位置；WindowServer双层位差为0也不能替代逐帧玻璃边缘跟手测量。

本工具当前为人工专项，不自动进入发版链，也不自动降低台账风险。源码本机通过不冒充冻结包、无缓存冷启动、其他机型/系统、HID输入或长期稳定性验收。

录制与取证自身增加负载；这里的资源统计不用于宣称产品省电或性能改善。

输出可能含取景区附近的私人桌面内容，只留在本地，不上传、不随公开源码/发行资产发布。
