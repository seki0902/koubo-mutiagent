package main

import (
	"encoding/json"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"

	"koubo-video-tool/store"
)

func seedAvatars(dataDir string) {
	avatarsPath := filepath.Join(dataDir, "avatars.json")

	// 只在空文件时自动填充，避免覆盖用户手动编辑
	existing, err := store.LoadAvatars(avatarsPath)
	if err != nil || len(existing) > 0 {
		return
	}

	entries, err := fs.ReadDir(embedded, "chanjing/person id")
	if err != nil {
		return
	}

	var avatars []store.Avatar
	n := 0
	for _, e := range entries {
		if e.IsDir() || filepath.Ext(e.Name()) != ".png" {
			continue
		}
		// 文件名即形象 ID（形如 C-xxxxxxxx.png），去掉后缀就是接口需要的 ID
		name := e.Name()
		id := name[:len(name)-4] // 去掉 ".png"，保留 "C-xxx"
		n++
		avatars = append(avatars, store.Avatar{
			ID:         id,
			Name:       fmt.Sprintf("%d", n),
			PreviewURL: "/img/person/" + name,
		})
	}

	b, err := json.MarshalIndent(avatars, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "警告: 形象数据序列化失败: %v\n", err)
		return
	}
	if err := os.WriteFile(avatarsPath, b, 0644); err != nil {
		fmt.Fprintf(os.Stderr, "警告: 形象数据写入失败: %v\n", err)
	}
}

func seedVoices(dataDir string) {
	voicesPath := filepath.Join(dataDir, "voices.json")
	existing, err := store.LoadVoices(voicesPath)
	if err != nil || len(existing) > 0 {
		return
	}
	voices := []store.Voice{
		{
			ID:          "C-fa39b63eaefa4d3689526a1dfd5a25f3",
			Name:        "系统人声（稳定）",
			Gender:      "女",
			Description: "蝉镜系统 TTS",
		},
		{
			ID:          "C-06d6082c2f39471b8cd28a2f1585b170",
			Name:        "自定义人声",
			Gender:      "女",
			Description: "Web 自定义音色",
			AudioSource: 1,
		},
	}
	b, err := json.MarshalIndent(voices, "", "  ")
	if err != nil {
		fmt.Fprintf(os.Stderr, "警告: 人声数据序列化失败: %v\n", err)
		return
	}
	if err := os.WriteFile(voicesPath, b, 0644); err != nil {
		fmt.Fprintf(os.Stderr, "警告: 人声数据写入失败: %v\n", err)
	}
}
