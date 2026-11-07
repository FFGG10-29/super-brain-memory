import 'dart:async';

import 'package:flutter/material.dart';

import 'app_shell.dart';
import 'backend_launcher.dart';
import 'prefs.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
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
