# 屏摄溯源桌面隐水印

> 面向 Windows 桌面的拍屏溯源水印原型：把不可见的鲁棒水印叠加到整个桌面，在截图、手机拍屏、局部屏幕、角度倾斜和复杂光照下恢复 32 位溯源载荷。

[English README](README.en.md) | [技术原理说明](docs/technical-design.md)

## 算法概览

当前实现是一套全屏平铺的盲检测隐水印。每个水印块独立承载同一 32 位载荷，载荷先经过 Hamming(7,4) 纠错编码，再映射到密钥驱动的低频 FSK 音调对。覆盖层以自适应残差方式叠加到桌面，解码端通过屏幕矫正、多尺度块搜索、频域匹配滤波和多块投票恢复载荷。

## 核心能力

- Windows 桌面透明置顶水印覆盖层
- 截屏、拍屏、部分屏溯源
- 自动屏幕边框识别、手动四角矫正、部分屏原图解码
- 低频相干 FSK 水印，适配手机重采样和显示器摩尔纹
- Hamming(7,4) 纠错和多水印块投票
- 本地 Web 溯源台：上传图片、登记载荷、记录历史、统计成功率
- 低置信诊断：区分可信结果、候选载荷、水印过弱或缺失

## 技术路线

```text
32 位载荷
  -> Hamming(7,4) 纠错编码
  -> 密钥驱动的低频 FSK 音调对
  -> 全屏平铺自适应残差水印
  -> 拍屏/截图/局部图像
  -> 自动矫正或部分屏搜索
  -> 匹配滤波 + 多块投票
  -> 可信载荷或低置信诊断
```

## 安装

```powershell
python -m pip install -r requirements.txt
```

也可以开发模式安装命令行入口：

```powershell
python -m pip install -e .
```

## 快速自测

```powershell
python -m screen_watermark.selftest
```

自测会生成合成桌面、叠加水印，并验证全图、裁切、缩放、模糊、FSK 相机模式等场景。

## 启动桌面水印

推荐拍屏测试使用相机模式：

```powershell
python -m screen_watermark.overlay --payload 1a2b3c4e --camera-mode
```

相机模式默认：

```text
opacity   >= 0.055
amplitude >= 12.0
tile-size = 256
grid-size = 64
refresh   = 2 秒
```

复杂背景或远距离拍屏可以增强水印残差：

```powershell
python -m screen_watermark.overlay --payload 1a2b3c4e --camera-mode --amplitude 14
```

参数含义：

- `--opacity`：透明覆盖窗口的不透明度，默认相机模式最低 0.055。
- `--amplitude`：最终写入画面的像素级水印残差强度，拍屏鲁棒性主要由它决定。
- `--refresh-interval`：自适应水印刷新周期，避免窗口内容变化后出现旧底图残留。

## 命令行解码

自动识别屏幕边框并矫正：

```powershell
python -m screen_watermark.decode photo.jpg --camera-mode --auto-screen
```

手动输入四角：

```powershell
python -m screen_watermark.decode photo.jpg --camera-mode --corners "120,80 1820,140 1760,1010 90,960"
```

图形化点选四角：

```powershell
python -m screen_watermark.pick_corners photo.jpg
```

四角顺序建议为左上、右上、右下、左下；服务端会自动重排，但规范点选能减少异常样本。

## 本地溯源服务

```powershell
python -m screen_watermark.trace_server --port 8766
```

打开：

```text
http://127.0.0.1:8766/
```

Web 溯源台支持三种模式：

- **全屏自动**：优先识别屏幕边框并做透视矫正。
- **手动四角**：用户点选四角后按屏幕平面矫正。
- **部分屏**：不依赖边框，直接对局部图像做多尺度水印搜索。

服务会把每次溯源记录写入本地 SQLite：

```text
artifacts/trace_service.sqlite3
```

`status=ok` 才代表可信溯源；`low_confidence` 只表示候选结果，不应作为最终追责依据。

## 工程结构

```text
screen_watermark/
  core.py          # 水印生成、FSK 解码、纠错和投票
  overlay.py       # Windows 透明置顶桌面覆盖层
  decode.py        # 命令行离线解码
  trace_server.py  # Flask 本地溯源服务
  web/             # 中文 Web 溯源台
docs/
  technical-design.md
```

运行产物、上传图片、数据库和自测图片都会写入 `artifacts/`，已被 `.gitignore` 排除。

## 使用建议

- 拍屏优先用 `--camera-mode`。
- 复杂壁纸、强光、远距离拍摄建议 `--amplitude 14` 或 `16`。
- 部分屏尽量保留更多连续桌面区域，至少覆盖多个完整水印块。
- 若提示“疑似未包含有效水印”，先确认 overlay 仍在最上层，并等待 2 秒刷新后重新采集。
- 生产环境应把 32 位载荷映射到用户、设备、会话、时间等服务端登记信息，不要直接暴露个人信息。

## 适用边界

这是一个可落地原型，不是商用 DLP 全家桶。它适合做内网终端溯源、算法验证、拍屏鲁棒性实验和产品 PoC。真实部署还需要结合权限控制、进程保护、屏幕录制策略、密钥轮换、审计日志和误报复核流程。
