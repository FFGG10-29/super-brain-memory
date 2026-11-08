import 'dart:async';
import 'dart:convert';
import 'dart:ffi' as ffi;
import 'dart:io';

import 'package:flutter/foundation.dart';

/// Windows 原生窗口控制 + 单实例锁，**零 pub 依赖**。
///
/// 不引入 `window_manager` / `tray_manager` 之类的插件：它们需要 C++ 编译，
/// 而本项目发行包在没装 MSVC 的机器上也要能先跑起来。这里只用 dart:ffi
/// 调 user32 的 5 个函数，UTF-16 缓冲区用 msvcrt 的 malloc/free 手动分配，
/// 因此不需要 package:ffi 的 Utf16/calloc。
///
/// 窗口标题来自 windows/runner/main.cpp：`window.Create(L"super_brain", ...)`。
class Win32Window {
  Win32Window._();

  static const int _swHide = 0;
  static const int _swShow = 5;
  static const int _swRestore = 9;
  static const int _wmAppQuit = 0x8002; // WM_APP+2：runner 里「完全退出」
  static const int _wmAppTrayMode = 0x8003; // WM_APP+3：runner 里切换关窗隐藏

  static ffi.DynamicLibrary? _user32;
  static ffi.DynamicLibrary? _msvcrt;

  static bool get supported => Platform.isWindows;

  static ffi.DynamicLibrary get _u => _user32 ??= ffi.DynamicLibrary.open('user32.dll');
  static ffi.DynamicLibrary get _c => _msvcrt ??= ffi.DynamicLibrary.open('msvcrt.dll');

  static final _malloc = _c.lookupFunction<
      ffi.Pointer<ffi.Void> Function(ffi.UintPtr),
      ffi.Pointer<ffi.Void> Function(int)>('malloc');
  static final _free = _c.lookupFunction<
      ffi.Void Function(ffi.Pointer<ffi.Void>),
      void Function(ffi.Pointer<ffi.Void>)>('free');
  static final _findWindow = _u.lookupFunction<
      ffi.Pointer<ffi.Void> Function(ffi.Pointer<ffi.Uint16>, ffi.Pointer<ffi.Uint16>),
      ffi.Pointer<ffi.Void> Function(ffi.Pointer<ffi.Uint16>, ffi.Pointer<ffi.Uint16>)>('FindWindowW');
  static final _showWindow = _u.lookupFunction<
      ffi.Int32 Function(ffi.Pointer<ffi.Void>, ffi.Int32),
      int Function(ffi.Pointer<ffi.Void>, int)>('ShowWindow');
  static final _setForeground = _u.lookupFunction<
      ffi.Int32 Function(ffi.Pointer<ffi.Void>),
      int Function(ffi.Pointer<ffi.Void>)>('SetForegroundWindow');
  static final _isIconic = _u.lookupFunction<
      ffi.Int32 Function(ffi.Pointer<ffi.Void>),
      int Function(ffi.Pointer<ffi.Void>)>('IsIconic');
  static final _isVisible = _u.lookupFunction<
      ffi.Int32 Function(ffi.Pointer<ffi.Void>),
      int Function(ffi.Pointer<ffi.Void>)>('IsWindowVisible');
  static final _postMessage = _u.lookupFunction<
      ffi.Int32 Function(ffi.Pointer<ffi.Void>, ffi.Uint32, ffi.UintPtr, ffi.IntPtr),
      int Function(ffi.Pointer<ffi.Void>, int, int, int)>('PostMessageW');

  static ffi.Pointer<ffi.Uint16> _utf16(String text) {
    final units = text.codeUnits;
    final pointer = _malloc((units.length + 1) * ffi.sizeOf<ffi.Uint16>()).cast<ffi.Uint16>();
    final buffer = pointer.asTypedList(units.length + 1);
    for (var i = 0; i < units.length; i++) {
      buffer[i] = units[i];
    }
    buffer[units.length] = 0;
    return pointer;
  }

  static ffi.Pointer<ffi.Void>? get handle {
    if (!supported) return null;
    final title = _utf16('super_brain');
    try {
      final hwnd = _findWindow(ffi.nullptr.cast<ffi.Uint16>(), title);
      return hwnd.address == 0 ? null : hwnd;
    } finally {
      _free(title.cast<ffi.Void>());
    }
  }

  /// 是否存在主窗口。单实例自愈靠它判断「占着端口的是不是僵尸」。
  static bool get exists => handle != null;

  static bool get visible {
    final hwnd = handle;
    return hwnd != null && _isVisible(hwnd) != 0;
  }

  /// 托盘菜单 / 第二实例唤起：最小化时先 restore，再显示并置前。
  static bool show() {
    final hwnd = handle;
    if (hwnd == null) return false;
    _showWindow(hwnd, _isIconic(hwnd) != 0 ? _swRestore : _swShow);
    _setForeground(hwnd);
    return true;
  }

  static bool hide() {
    final hwnd = handle;
    if (hwnd == null) return false;
    return _showWindow(hwnd, _swHide) != 0;
  }

  /// 托盘「完全退出」：runner 的 WM_CLOSE 已被拦截为隐藏，走专用消息真退出。
  static bool quit() {
    final hwnd = handle;
    if (hwnd == null) return false;
    return _postMessage(hwnd, _wmAppQuit, 0, 0) != 0;
  }

  /// 启用托盘后告诉 runner「关窗只隐藏」；停用托盘时恢复关窗即退出。
  static bool setTrayMode(bool hideOnClose) {
    final hwnd = handle;
    if (hwnd == null) return false;
    return _postMessage(hwnd, _wmAppTrayMode, hideOnClose ? 1 : 0, 0) != 0;
  }
}

/// 单实例：固定本地端口当锁。第二个实例连上后发 `show`，第一个实例唤起窗口。
/// 端口只绑 127.0.0.1，不对外暴露，也不影响后端 8766/8767/8768。
class SingleInstance {
  SingleInstance._();

  static const int _lockPort = 38767;
  static ServerSocket? _server;

  /// 抢占单实例锁。
  ///
  /// 历史坑：`claim()` 在 `runApp()` 之前绑定端口；若之后 Dart 初始化抛异常，
  /// 端口监听会让 isolate 一直活着，进程留在后台却**没有窗口**（僵尸）。
  /// 之后每次双击都被当成第二实例，发完 show 就退出 → 表现就是「软件打不开」。
  /// 现在：第二实例发现端口被占但系统里找不到主窗口时，强杀僵尸并重新抢锁。
  static Future<bool> claim(void Function(String command) onCommand) async {
    for (var attempt = 0; attempt < 3; attempt++) {
      try {
        _server = await ServerSocket.bind(InternetAddress.loopbackIPv4, _lockPort);
        _server!.listen((Socket socket) {
          socket.cast<List<int>>().transform(utf8.decoder).listen(
            (String data) {
              final command = data.trim();
              if (command.isNotEmpty) onCommand(command);
              socket.destroy();
              // 自己连窗口都没有了 → 主动退位，别再占着锁挡住下一个实例。
              if (command == 'show' && !Win32Window.exists) {
                Future<void>.delayed(const Duration(milliseconds: 250), () {
                  release();
                  exit(0);
                });
              }
            },
            onError: (Object _) => socket.destroy(),
            onDone: () => socket.destroy(),
          );
        });
        return true;
      } on SocketException {
        final hadWindow = Win32Window.exists;
        try {
          final socket = await Socket.connect(
            InternetAddress.loopbackIPv4.address, _lockPort,
            timeout: const Duration(seconds: 2),
          );
          socket.write('show');
          await socket.flush();
          await Future<void>.delayed(const Duration(milliseconds: 450));
          socket.destroy();
        } on Object catch (_) {
          // 连接不上也继续走僵尸判定
        }
        if (hadWindow || Win32Window.exists) {
          return false; // 正常情况：主实例有窗口，已唤起，本实例退出
        }
        // 端口被占但没有任何主窗口 = 僵尸，强杀后重试抢锁
        await _killPortOwner();
        await Future<void>.delayed(const Duration(milliseconds: 350));
      }
    }
    return false;
  }

  /// 找到 LISTENING 在单实例端口上的进程并结束它（仅僵尸场景调用）。
  static Future<void> _killPortOwner() async {
    try {
      final result = await Process.run(
        'netstat', <String>['-ano', '-p', 'TCP'],
        stdoutEncoding: systemEncoding,
      );
      final text = result.stdout?.toString() ?? '';
      for (final line in text.split('\n')) {
        if (!line.contains('127.0.0.1:$_lockPort')) continue;
        final parts = line.trim().split(RegExp(r'\s+'));
        if (parts.length < 5 || parts[3].toUpperCase() != 'LISTENING') continue;
        final pid = int.tryParse(parts.last);
        if (pid != null && pid > 0) Process.killPid(pid, ProcessSignal.sigkill);
      }
    } on Object catch (_) {
      // 拿不到 pid 也无妨，下一轮 claim 仍会重试
    }
  }

  static void release() {
    _server?.close();
    _server = null;
  }
}

/// 后端托盘菜单动作的节流日志（与状态栏 ValueNotifier 复用同一套）。
final ValueNotifier<String> trayStatus = ValueNotifier<String>('托盘未启用');
