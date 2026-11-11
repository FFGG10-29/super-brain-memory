; Inno Setup 6 script - Super Brain: Flutter frontend + frozen Python backend
; build: ISCC.exe setup.iss   (run build.ps1 first to prepare dist\)
[Setup]
AppId={{8E7B2C10-5B1C-4C6B-9F1A-3D7E9A0C2B11}
AppName=Super Brain
AppVersion=3.1.0
AppPublisher=Super Brain
DefaultDirName={autopf}\SuperBrain
DefaultGroupName=Super Brain
DisableProgramGroupPage=yes
; 安装时必须允许用户选择落地位置（默认即为 no，此处显式声明以防被覆盖）
DisableDirPage=no
AlwaysShowDirOnReadyPage=yes
OutputDir=..\..\release
OutputBaseFilename=SuperBrainSetup
; 单文件本地分发：降低 lzma2 级别并关闭固实压缩，压缩内存/CPU 压力大幅下降。
; 原因：lzma2/max + solid 压缩 4GB 时会与常驻后端抢资源，后端曾崩在 ucrtbase.dll。
; 包体略大几十 MB，换来压缩时间减半且 MCP 服务可保持在线。
; 【2026-11-10 提速】9.6G 打包源里 9.2G 是高熵二进制（ggml-cuda.dll 873M /
; cublasLt 452M / cufft 274M / models 3.3G...），lzma2 压它们几乎不缩小却极慢，
; 导致后台构建任务（单次上限 10 分钟）两次被超时强杀。故：这些大文件走
; nocompression 直接存储，仅对约 460M 的小文件（pyd/zip/py）做 lzma2/fast。
; 代价：安装包从约 4.2G 涨到约 9.6G，换来压缩从 30 分钟降到分钟级。
Compression=lzma2/fast
SolidCompression=no
; 全量包（含 3.3GB 模型权重）压缩后 >4.2GB，Inno 单文件 Setup.exe 不支持，
; 必须开磁盘分卷：输出 SuperBrainSetup.exe + SuperBrainSetup-1.bin / -2.bin …
; 每卷 1.9GB，正好低于 GitHub Release 单资源 2GB 的限制。
DiskSpanning=yes
DiskSliceSize=1900000000
WizardStyle=modern
; Flutter 前端的品牌图标（多尺寸 16~256）
SetupIconFile=..\..\flutter_app\windows\runner\resources\app_icon.ico
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=admin
; 允许普通用户选择"仅为我安装"，此时可写入非 Program Files 的自定义目录
PrivilegesRequiredOverridesAllowed=dialog
UninstallDisplayIcon={app}\super_brain.exe

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create desktop shortcut"; GroupDescription: "Shortcuts:"

[Files]
; dist\ contains super_brain.exe + data\ (Flutter) and backend\ (frozen headless server).
; 必须排除两个 PyInstaller 中间产物目录：super-brain\ 是旧版整体冻结包，
; super-brain-backend\ 是后端构建产物（已复制到 dist\backend\）。漏排会把 20GB+ 的旧
; 包内容打进安装包。
; 【提速】backend\_internal 整目录（9.6G：3.3G 模型 + 5.9G 高熵 CUDA DLL）
; 直接存储 nocompression：这些二进制压缩收益近零却是耗时主因，lzma2 硬压会让
; 后台构建任务（单次上限 10 分钟）被超时强杀。用 skipifsourcedoesntexist 容错。
Source: "..\..\dist\backend\_internal\*"; DestDir: "{app}\backend\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression skipifsourcedoesntexist
; 其余文件（前端 exe/dll + backend\headless-server*.exe + 图标）走 lzma2/fast 压缩；
; 排除 _internal 避免重复打包，排除两个 PyInstaller 中间产物目录。
Source: "..\..\dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "super-brain\*,super-brain-backend\*,backend\_internal\*,winsw.exe,super-brain-v3.xml,*.log,*.pyc"

; 不预建数据目录：core/runtime_paths.py::user_dir()/data_dir() 启动时自行 mkdir，
; 且冻结态数据落 %APPDATA%\super-brain\data（每用户区）。安装以 admin 运行时预建
; 每用户目录会指向错误的用户并触发 Inno Setup 的 UsedUserAreasWarning。

[Run]
; Flutter starts first and launches backend\headless-server.exe itself (process separation).
; runasoriginaluser：安装若提权，仍以当前登录用户启动，避免数据写到管理员的 APPDATA。
Filename: "{app}\super_brain.exe"; Description: "Launch Super Brain"; Flags: nowait postinstall skipifsilent shellexec runasoriginaluser

[Icons]
Name: "{group}\Super Brain"; Filename: "{app}\super_brain.exe"
Name: "{autodesktop}\Super Brain"; Filename: "{app}\super_brain.exe"; Tasks: desktopicon

[Code]
// Memory data is intentionally NOT removed on uninstall (user memory survives).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
    MsgBox('Your memory data in ' + ExpandConstant('{userappdata}\super-brain\data') + ' was kept.', mbInformation, MB_OK);
end;
