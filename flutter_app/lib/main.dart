import 'dart:async';
import 'dart:io';

import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'backend_launcher.dart';
import 'prefs.dart';
import 'win32.dart';
import 'win32_job.dart';

Future<void> main(List<String> args) async {
  WidgetsFlutterBinding.ensureInitialized();
  // 单实例：第二个实例只会把已有窗口拉回前台，然后自己退出（后端六引擎不会被抢锁）。
  final first = await SingleInstance.claim((String command) {
    if (command == 'show') Win32Window.show();
  });
  if (!first) exit(0);

  // 关键：claim() 已经绑定了 38767 端口监听，Dart 事件循环会因此一直活着。
  // 若启动阶段抛异常，进程会变成「占着单实例锁却没有窗口」的僵尸，
  // 之后每次双击都被当成第二实例退出 → 表现就是「软件打不开 / 僵尸老是有」。
  // 但只有「启动阶段」的异常才该终止进程：runApp 之后某个页面取数失败
  // （后端离线时返回结构不同等）绝不能连累整个应用一起退出。
  var started = false;
  runZonedGuarded(() {
    // Job Object：把本进程（以及随后拉起的后端）绑进同一个 job。
    // 前端被强杀/崩溃时，系统关闭 job 句柄会一并终止后端，杜绝孤儿进程锁死数据库。
    Win32Job.install();

    Prefs.load();
    // 进程分离：先探活，后端没起来就拉起独立 Python 进程（不阻塞 UI 首帧）。
    unawaited(
      BackendLauncher.ensureRunning(
        host: Prefs.get<String>('host', '127.0.0.1'),
        httpPort: Prefs.get<int>('http_port', 8768),
      ).catchError((Object error) {
        backendStatus.value = '后端拉起异常：$error';
        return backendStatus.value;
      }),
    );
    runApp(const SuperBrainApp());
    started = true;

    // `--tray` 来自开机自启项：开机只驻留托盘，不弹窗。
    // 稍等窗口创建完成再隐藏；app_shell 启用托盘后还会再确认一次隐藏模式。
    if (args.contains('--tray')) {
      unawaited(Future<void>.delayed(const Duration(milliseconds: 1500), () {
        Win32Window.setTrayMode(true);
        Win32Window.hide();
      }));
    }
  }, (Object error, StackTrace stack) {
    try {
      final base = Platform.environment['APPDATA'] ?? '.';
      final log = File('$base${Platform.pathSeparator}super-brain'
          '${Platform.pathSeparator}startup_error.log');
      log.parent.createSync(recursive: true);
      final phase = started ? 'RUNTIME (已忽略，应用继续运行)' : 'STARTUP (致命)';
      log.writeAsStringSync('${DateTime.now()} [$phase]\n$error\n$stack\n',
          mode: FileMode.append);
    } on Object catch (_) {}
    if (!started) {
      // 仅启动失败才释放单实例锁并退出；运行期异常只记日志，界面保持存活。
      SingleInstance.release();
      exit(1);
    }
  });
}

class SuperBrainApp extends StatelessWidget {
  const SuperBrainApp({super.key});

  @override
  Widget build(BuildContext context) => MaterialApp(
        title: 'Super Brain v3',
        debugShowCheckedModeBanner: false,
        theme: ThemeData(
          brightness: Brightness.dark,
          scaffoldBackgroundColor: const Color(0xFF05080E),
          colorScheme: const ColorScheme.dark(
            primary: Color(0xFF4FC3F7),
            secondary: Color(0xFF7FE9FF),
            surface: Color(0xFF0A1424),
          ),
          fontFamily: 'Microsoft YaHei UI',
        ),
        home: const AppShell(),
      );
}
