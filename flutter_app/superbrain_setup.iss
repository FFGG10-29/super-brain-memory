; Super Brain Windows 安装包（Inno Setup 6）—— 与旧版 3.1.0 同身份：前端 + frozen 后端
; 2026-11-14 重建：前端 flutter build windows --release（含「最大回复条数」UI）
; 后端用 super-brain-backend-lite.spec 重新 PyInstaller 编译（含 L0/回复条数上限修复），
; 云端化后本地推理运行时已在 spec 的 excludes 里裁掉，backend-lite 仅 735MB，无 6.2G 死重。
#define MyAppName "Super Brain"
#define MyAppVersion "3.1.0"
#define MyAppExe "super_brain.exe"
#define ReleaseDir "B:\BaiduSyncdisk\desktop\VSProject\super-brain-memory\flutter_app\build\windows\x64\runner\Release"
#define BackendDir "D:\SuperBrain\backend-lite"

[Setup]
AppId={{8E7B2C10-5B1C-4C6B-9F1A-3D7E9A0C2B11}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
DefaultDirName=D:\SuperBrain
DefaultGroupName=Super Brain
UninstallDisplayIcon={app}\{#MyAppExe}
OutputDir=D:\SuperBrain\installer
OutputBaseFilename=Super-Brain-{#MyAppVersion}-setup
Compression=lzma2/normal
SolidCompression=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
WizardStyle=modern

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Files]
Source: "{#ReleaseDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#BackendDir}\headless-server.exe"; DestDir: "{app}\backend"; Flags: ignoreversion
Source: "{#BackendDir}\headless-server-cli.exe"; DestDir: "{app}\backend"; Flags: ignoreversion
Source: "{#BackendDir}\app.ico"; DestDir: "{app}\backend"; Flags: ignoreversion
Source: "{#BackendDir}\_internal\*"; DestDir: "{app}\backend\_internal"; Flags: ignoreversion recursesubdirs createallsubdirs; Excludes: "onnxruntime,llama_cpp,ggml-cuda.dll,cublas64_12.dll,cublasLt64_12.dll,cudnn_*,cufft64_11.dll,nvrtc64*.dll,cufftw64_11.dll,models"

[Icons]
Name: "{group}\Super Brain"; Filename: "{app}\{#MyAppExe}"
Name: "{autodesktop}\Super Brain"; Filename: "{app}\{#MyAppExe}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExe}"; Description: "立即启动 Super Brain"; Flags: nowait postinstall skipifsilent
