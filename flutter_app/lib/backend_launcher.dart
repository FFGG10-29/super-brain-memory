import 'dart:io';

import 'package:flutter/foundation.dart';

import 'prefs.dart';

/// 后端拉起状态（状态栏实时显示）。
final ValueNotifier<String> backendStatus = ValueNotifier<String>('后端探活中…');

/// 进程分离：Flutter 只做 UI，后端是独立的 Python 进程。
///
/// 生命周期约定：
/// - 启动时创建 Job Object（见 win32_job.dart），后端子进程自动入组；
///   前端被强杀时由系统一并终止后端，绝不残留。
/// - 只记录「本实例亲自拉起」的 PID。若探活发现后端已在跑（别的实例/手动启动），
///   托盘「完全退出」不会去杀它，避免误伤。
class BackendLauncher {
  static int? ownedPid;
  static String ownedHost = '127.0.0.1';
  static int ownedHttpPort = 8768;

  static Future<bool> isAlive(String host, int port) async {
    HttpClient? client;
    try {
      client = HttpClient()..connectionTimeout = const Duration(milliseconds: 800);
      final request = await client.getUrl(Uri.parse('http://$host:$port/v1/health'));
      final response = await request.close().timeout(const Duration(seconds: 2));
      await response.drain<void>();
      return response.statusCode == 200;
    } catch (_) {
      return false;
    } finally {
      client?.close(force: true);
    }
  }

  /// 优先级：安装包里的 backend/headless-server.exe > 开发态 venv python > PATH python。
  static Map<String, String> resolveDefaults() {
    final exeName = Platform.isWindows ? 'headless-server.exe' : 'headless-server';
    final starts = <String>[
      File(Platform.resolvedExecutable).parent.path,
      Directory.current.path,
    ];
    for (final start in starts) {
      var dir = Directory(start);
      for (var depth = 0; depth < 6; depth++) {
        // ① 安装布局：<root>\super_brain.exe 与 <root>\backend\headless-server.exe 同级
        final exe = File('${dir.path}${Platform.pathSeparator}backend${Platform.pathSeparator}$exeName');
        if (exe.existsSync()) {
          return <String, String>{
            'command': exe.path,
            'directory': exe.parent.path,
            'args': '',
          };
        }
        // ② 开发态：项目根有 headless_server.py，用 venv 的 python -m headless_server
        final marker = File('${dir.path}${Platform.pathSeparator}headless_server.py');
        if (marker.existsSync()) {
          final venv = Platform.isWindows
              ? '${dir.path}${Platform.pathSeparator}.venv${Platform.pathSeparator}Scripts${Platform.pathSeparator}python.exe'
              : '${dir.path}${Platform.pathSeparator}.venv${Platform.pathSeparator}bin${Platform.pathSeparator}python';
          final python = File(venv).existsSync() ? venv : (Platform.isWindows ? 'python' : 'python3');
          return <String, String>{
            'command': python,
            'directory': dir.path,
            'args': '-m headless_server',
          };
        }
        if (dir.parent.path == dir.path) break;
        dir = dir.parent;
      }
    }
    return <String, String>{
      'command': Platform.isWindows ? 'python' : 'python3',
      'directory': Directory.current.path,
      'args': '-m headless_server',
    };
  }

  static Future<String> ensureRunning({required String host, required int httpPort}) async {
    ownedHost = host;
    ownedHttpPort = httpPort;
    if (await isAlive(host, httpPort)) {
      backendStatus.value = '后端已运行（独立进程）';
      return backendStatus.value;
    }
    if (!Prefs.get<bool>('auto_start_backend', true)) {
      backendStatus.value = '自动拉起已关闭；请手动运行 backend\\headless-server.exe 或 python -m headless_server';
      return backendStatus.value;
    }
    final defaults = resolveDefaults();
    final command = _or(Prefs.get<String>('backend_command', ''), defaults['command']!);
    final directory = _or(Prefs.get<String>('backend_dir', ''), defaults['directory']!);
    var args = _or(Prefs.get<String>('backend_args', ''), defaults['args']!);
    // 兼容旧偏好：frozen headless-server.exe 不吃 '-m headless_server'，命中才清空。
    // 不能用 endsWith('.exe') 判定——python/pythonw 直启（-m 模式）也是 .exe，
    // 2026-11-13 实测该规则把 pythonw 的启动参数清空，导致后端空挂 30s 超时。
    if (command.toLowerCase().contains('headless-server.exe') && args.contains('-m ')) args = '';
    backendStatus.value = '正在拉起后端…';
    // P0-1：显式注入模型目录（D 盘），确保 reranker/embedder 解析到真 ONNX 权重，
    // 而不是靠进程内部兜底或退化成 IdentityReranker（无精排）。
    // 后端代码层 create_reranker/models_dir 已有互补回退，这里是启动侧双保险；
    // 已存在同名环境变量时不覆盖（尊重手动排障/自定义模型目录）。
    final modelsDir = Prefs.get<String>('models_dir', '').trim();
    final resolvedModels = modelsDir.isNotEmpty
        ? modelsDir
        : r'D:\SuperBrain\backend\_internal\models';
    final env = <String, String>{};
    if (Platform.isWindows && Directory(resolvedModels).existsSync()) {
      env['SUPER_BRAIN_MODELS_DIR'] = resolvedModels;
    }
    try {
      final process = await Process.start(
        command, _splitArgs(args),
        workingDirectory: directory,
        mode: ProcessStartMode.detached,
        includeParentEnvironment: true,
        environment: env,
      );
      ownedPid = process.pid;
    } catch (error) {
      backendStatus.value = '后端拉起失败：$error';
      return backendStatus.value;
    }
    for (var attempt = 0; attempt < 60; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 500));
      if (await isAlive(host, httpPort)) {
        backendStatus.value = '后端已拉起（独立进程）';
        return backendStatus.value;
      }
    }
    backendStatus.value = '后端启动超时 30s；请在设置页检查启动命令与目录';
    return backendStatus.value;
  }

  /// 托盘「完全退出」：优雅停机 RPC 已先行发出，这里等最多 3 秒确认端口关闭；
  /// 仍未退出则按 PID 兜底终止（只针对本实例亲自拉起的进程）。
  static Future<void> stopOwned() async {
    final pid = ownedPid;
    if (pid == null) return;
    for (var attempt = 0; attempt < 6; attempt++) {
      await Future<void>.delayed(const Duration(milliseconds: 500));
      if (!await isAlive(ownedHost, ownedHttpPort)) {
        ownedPid = null;
        backendStatus.value = '后端已优雅退出';
        return;
      }
    }
    try {
      Process.killPid(pid);
      backendStatus.value = '后端已按 PID 兜底终止';
    } catch (_) {
      backendStatus.value = '后端终止失败，请检查任务管理器';
    }
    ownedPid = null;
  }

  static String _or(String value, String fallback) => value.trim().isEmpty ? fallback : value.trim();

  /// 简单命令行切分：支持双引号包裹的含空格参数。
  static List<String> _splitArgs(String raw) {
    final result = <String>[];
    final buffer = StringBuffer();
    var quoted = false;
    for (final codeUnit in raw.codeUnits) {
      final char = String.fromCharCode(codeUnit);
      if (char == '"') {
        quoted = !quoted;
      } else if (char == ' ' && !quoted) {
        if (buffer.isNotEmpty) result.add(buffer.toString());
        buffer.clear();
      } else {
        buffer.write(char);
      }
    }
    if (buffer.isNotEmpty) result.add(buffer.toString());
    return result;
  }
}
