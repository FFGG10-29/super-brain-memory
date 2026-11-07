import 'dart:io';

import 'package:flutter/foundation.dart';

import 'prefs.dart';

/// 后端拉起状态（状态栏实时显示）。
final ValueNotifier<String> backendStatus = ValueNotifier<String>('后端探活中…');

/// 进程分离：Flutter 只做 UI，后端是独立的 Python 进程。
/// 启动时先探活 /v1/health；没起来才按配置拉起并轮询等待，之后不再管它（detached 常驻）。
class BackendLauncher {
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
    if (await isAlive(host, httpPort)) {
      backendStatus.value = '后端已运行（独立进程）';
      return backendStatus.value;
    }
    if (!Prefs.get<bool>('auto_start_backend', true)) {
      backendStatus.value = '自动拉起已关闭；请手动运行 backend\headless-server.exe 或 python -m headless_server';
      return backendStatus.value;
    }
    final defaults = resolveDefaults();
    final command = _or(Prefs.get<String>('backend_command', ''), defaults['command']!);
    final directory = _or(Prefs.get<String>('backend_dir', ''), defaults['directory']!);
    var args = _or(Prefs.get<String>('backend_args', ''), defaults['args']!);
    // 兼容旧偏好：以前默认把 '-m headless_server' 写进 prefs，传给 exe 会失败
    if (command.toLowerCase().endsWith('.exe') && args.contains('-m ')) args = '';
    backendStatus.value = '正在拉起后端…';
    try {
      await Process.start(
        command, _splitArgs(args),
        workingDirectory: directory,
        mode: ProcessStartMode.detached,
        includeParentEnvironment: true,
      );
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
