package main

import (
	"context"
	"embed"
	"flag"
	"fmt"
	"io/fs"
	"net/http"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"koubo-video-tool/chanjing"
	"koubo-video-tool/handlers"
	"koubo-video-tool/scheduler"
	"koubo-video-tool/store"
)

// 这个仓库只带 Agent 应用需要的资源：前端页面与技能占位目录。
// 形象预览图属于原工具的形象选择页，未包含在内；形象列表由接口实时获取。
//
//go:embed frontend/* skills/*
var embedded embed.FS

// servable reports whether the embedded frontend holds a real file, so a missing
// path can be answered with the friendly page rather than a bare 404.
func servable(frontendFS fs.FS, path string) bool {
	if path == "" {
		return true
	}
	info, err := fs.Stat(frontendFS, path)
	return err == nil && !info.IsDir()
}

// pageNotFoundHTML names the application that answered and links to both homes.
func pageNotFoundHTML(mode handlers.AppMode) string {
	app, home, other, otherName := "口播工具", "/", "http://127.0.0.1:8909", "Agent 应用"
	if mode == handlers.ModeAgent {
		app, other, otherName = "Agent 应用", "http://127.0.0.1:8899", "口播工具"
	}
	return `<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>页面不存在</title><link rel="stylesheet" href="/style.css"></head>
<body><div class="page">
<div class="page-title">页面不存在</div>
<p style="color:#86868b;">当前运行的是<b>` + app + `</b>，这个地址不属于它。可能是旧链接或书签
指向了另一个应用的页面——两个应用已经分成各自独立的端口和配置。</p>
<p><a class="btn-p" href="` + home + `">回到首页</a>
<a class="btn-s" href="` + other + `">打开` + otherName + `（` + other + `）</a></p>
</div></body>
</html>`
}

func main() {
	printCreds := flag.Bool("print-creds", false, "打印解密后的蝉镜凭据并退出（调试用）")
	testChanjing := flag.Bool("test-chanjing", false, "使用 n8n 工作流同款硬编码参数直接调蝉镜 API（调试用）")
	flag.Parse()

	exeFile := executablePath()
	exeDir := filepath.Dir(exeFile)
	dataDir := runtimeDataDir(exeDir)
	os.MkdirAll(dataDir, 0755)

	if *printCreds {
		cfg, err := store.LoadConfig(filepath.Join(dataDir, "config.json"))
		if err != nil {
			fmt.Fprintf(os.Stderr, "读取配置失败: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("APP_ID=%s\n", cfg.Chanjing.AppID)
		fmt.Printf("SECRET_KEY=%s\n", cfg.Chanjing.SecretKey)
		return
	}

	if *testChanjing {
		cfg, err := store.LoadConfig(filepath.Join(dataDir, "config.json"))
		if err != nil {
			fmt.Fprintf(os.Stderr, "读取配置失败: %v\n", err)
			os.Exit(1)
		}
		// 使用 n8n 工作流里硬编码的同款参数
		client := chanjing.NewClient(cfg.Chanjing.AppID, cfg.Chanjing.SecretKey)
		token, err := client.GetToken()
		if err != nil {
			fmt.Fprintf(os.Stderr, "获取 token 失败: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("Token 获取成功: %s...\n", token[:20])

		taskID, err := client.CreateVideo(token, chanjing.CreateVideoRequest{
			Person: chanjing.PersonConfig{
				ID:         "C-6a951f1434884368bc4942f7f74885ff",
				X:          0,
				Y:          0,
				Width:      1080,
				Height:     1920,
				FigureType: "stand_body",
				DriveMode:  "random",
				Backway:    1,
			},
			Audio: chanjing.AudioConfig{
				TTS: chanjing.TTSConfig{
					Text:     []string{"测试测试，这是一条测试稿子"},
					Speed:    1.2,
					AudioMan: "C-fa39b63eaefa4d3689526a1dfd5a25f3",
				},
				WavURL:   "",
				Type:     "tts",
				Volume:   100,
				Language: "cn",
			},
			BgColor:                "#ffffff",
			ScreenWidth:            1080,
			ScreenHeight:           1920,
			SubtitleConfig:         chanjing.SubtitleConfig{Show: false},
			AddComplianceWatermark: false,
			Source:                 1,
		})
		if err != nil {
			fmt.Fprintf(os.Stderr, "创建视频失败: %v\n", err)
			os.Exit(1)
		}
		fmt.Printf("✅ 成功！任务 ID: %s\n", taskID)
		return
	}

	initDataFiles(dataDir)
	seedAvatars(dataDir)
	seedVoices(dataDir)

	cfg, err := store.LoadConfig(filepath.Join(dataDir, "config.json"))
	if err != nil {
		fi, statErr := os.Stat(filepath.Join(dataDir, "config.json"))
		if statErr == nil && fi.Size() > 0 {
			fmt.Fprintf(os.Stderr, "警告: 配置文件损坏，将使用默认配置 (%v)\n", err)
		}
	}
	cjClient := chanjing.NewClient(cfg.Chanjing.AppID, cfg.Chanjing.SecretKey)

	sch := scheduler.New(cjClient, func(t store.Task) {
		tasksPath := filepath.Join(dataDir, "tasks.json")
		store.UpdateTask(tasksPath, t.TaskID, func(existing *store.Task) {
			existing.Status = t.Status
			existing.Progress = t.Progress
			existing.VideoURL = t.VideoURL
			existing.Error = t.Error
			existing.CompletedAt = t.CompletedAt
		})
	})

	// 恢复未完成任务
	tasks, _ := store.LoadTasks(filepath.Join(dataDir, "tasks.json"))
	sch.Restore(tasks)

	h := handlers.New(dataDir, cjClient, sch)
	mode := handlers.AppMode(strings.ToLower(strings.TrimSpace(os.Getenv("KOUBO_APP_MODE"))))
	h.RegisterMode(http.DefaultServeMux, mode)

	frontendFS, err := fs.Sub(embedded, "frontend")
	if err != nil {
		panic(fmt.Sprintf("嵌入资源加载失败 frontend: %v", err))
	}
	fileServer := http.FileServer(http.FS(frontendFS))
	// The frontend is embedded and rebuilt in place, so a browser must not keep
	// an older copy: a stale script running against newer markup throws on the
	// first missing element and the page then silently stops working (no reply
	// at all). Serving the assets with no-store removes that whole failure class.
	assets := http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store, must-revalidate")
		w.Header().Set("Pragma", "no-cache")
		fileServer.ServeHTTP(w, r)
	})
	// After the split each application serves only its own pages, so an old link
	// or bookmark can point at a page the other application owns. Explain that
	// instead of answering with Go's bare "404 page not found".
	notFound := func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		w.WriteHeader(http.StatusNotFound)
		fmt.Fprint(w, pageNotFoundHTML(mode))
	}
	switch mode {
	case handlers.ModeAgent:
		// The Agent app opens on the conversation. The classic tool's pages are
		// not part of it: its own tool deployment serves those. settings.html
		// still loads the setup.html fragment, so that one stays reachable.
		http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
			path := strings.TrimPrefix(r.URL.Path, "/")
			switch {
			case path == "" || path == "index.html":
				http.ServeFileFS(w, r, frontendFS, "agent.html")
			case path == "main.html" || path == "tutorial.html":
				notFound(w, r)
			case !servable(frontendFS, path):
				notFound(w, r)
			default:
				assets.ServeHTTP(w, r)
			}
		})
	case handlers.ModeTool:
		// Mirror image: the tool serves its own pages and not the Agent chat.
		http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
			path := strings.TrimPrefix(r.URL.Path, "/")
			switch {
			case path == "agent.html" || path == "agent.js":
				notFound(w, r)
			case !servable(frontendFS, path):
				notFound(w, r)
			default:
				assets.ServeHTTP(w, r)
			}
		})
	default:
		http.Handle("/", assets)
	}

	// 形象图片路由：仓库里没有这些预览图时跳过，不影响其它功能。
	if personFS, err := fs.Sub(embedded, "chanjing/person id"); err == nil {
		http.Handle("/img/person/", http.StripPrefix("/img/person/", http.FileServer(http.FS(personFS))))
	}

	port := runtimePort()
	url := fmt.Sprintf("http://localhost:%s", port)

	fmt.Printf("服务启动: %s\n", url)
	fmt.Printf("数据目录: %s\n", dataDir)

	// 升级版默认不修改桌面，也不自动打开浏览器。需要桌面集成时由启动器显式开启。
	if envBool("KOUBO_CREATE_DESKTOP_SHORTCUT") {
		createDesktopShortcut(exeFile)
	}
	if envBool("KOUBO_OPEN_BROWSER") {
		exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
	}

	srv := &http.Server{Addr: ":" + port}

	// 启动 HTTP 服务（goroutine）
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			fmt.Fprintf(os.Stderr, "服务启动失败: %v\n", err)
			os.Exit(1)
		}
	}()

	// 等待退出信号
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit

	fmt.Println("\n正在关闭...")
	sch.Stop()

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	srv.Shutdown(ctx)
	fmt.Println("已关闭")
}

func runtimeDataDir(exeDir string) string {
	if configured := os.Getenv("KOUBO_DATA_DIR"); configured != "" {
		return configured
	}
	return filepath.Join(exeDir, "data")
}

func runtimePort() string {
	if configured := os.Getenv("KOUBO_PORT"); configured != "" {
		return configured
	}
	return "8909"
}

func envBool(name string) bool {
	switch os.Getenv(name) {
	case "1", "true", "TRUE", "yes", "YES":
		return true
	default:
		return false
	}
}

func executablePath() string {
	p, err := os.Executable()
	if err != nil {
		p, _ = filepath.Abs(os.Args[0])
	}
	if p == "" {
		p, _ = filepath.Abs(os.Args[0])
	}
	return p
}

func createDesktopShortcut(exeFile string) {
	desktop := filepath.Join(os.Getenv("USERPROFILE"), "Desktop")
	if _, err := os.Stat(desktop); err != nil {
		return // 桌面路径不可用
	}

	shortcutPath := filepath.Join(desktop, "口播视频工具.bat")
	if _, err := os.Stat(shortcutPath); err == nil {
		return // 已存在
	}

	os.WriteFile(shortcutPath, []byte(desktopShortcutContent(exeFile)), 0644)
	fmt.Println("桌面快捷方式已创建")
}

func desktopShortcutContent(exeFile string) string {
	return fmt.Sprintf("@echo off\r\nchcp 65001 > nul\r\nstart \"\" \"%s\"\r\n", exeFile)
}

func initDataFiles(dir string) {
	files := map[string]string{
		"config.json":  "{}",
		"tasks.json":   "[]",
		"topics.json":  "[]",
		"avatars.json": "[]",
		"voices.json":  "[]",
	}
	for name, content := range files {
		p := filepath.Join(dir, name)
		if _, err := os.Stat(p); os.IsNotExist(err) {
			os.WriteFile(p, []byte(content), 0644)
		}
	}
}
