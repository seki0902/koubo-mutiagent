package skill

import (
	"crypto/sha1"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"koubo-video-tool/store"
)

var defaultLocalSkillRoots = []string{
	filepath.Join("skills", "Koubo-rewrite-skill-local"),
	filepath.Join("skills", "Koubo-rewrite-skill"),
}

// BuildPrompt 读取 Skill 源，按仓库规则拼接 system prompt。
// 返回值第二项是本次实际加载的文件列表，便于日志记录。
func BuildPrompt(cfg *store.Config, topic string) (string, []string, error) {
	if cfg == nil {
		return "", nil, fmt.Errorf("配置为空")
	}

	switch cfg.Skill.Source {
	case "local":
		prompt, files, err := buildFromLocal(cfg.Skill.LocalPath, topic)
		if err == nil {
			return prompt, files, nil
		}
		legacyPrompt, legacyFiles, legacyErr := readLegacyMarkdownTree(cfg.Skill.LocalPath)
		if legacyErr == nil {
			return legacyPrompt, legacyFiles, nil
		}
		return "", nil, err

	case "github":
		if cfg.Skill.GithubURL == "" {
			return "", nil, fmt.Errorf("GitHub Skill URL 未设置")
		}
		return readGitHub(cfg, topic)

	default:
		return "", nil, fmt.Errorf("未知 Skill 源类型: %s", cfg.Skill.Source)
	}
}

func buildFromLocal(path, topic string) (string, []string, error) {
	root, err := resolveSkillRoot(path)
	if err != nil {
		return "", nil, err
	}
	return buildPromptFromDir(root, topic)
}

func buildPromptFromDir(root, topic string) (string, []string, error) {
	root, err := resolveSkillRoot(root)
	if err != nil {
		return "", nil, err
	}

	var sections []string
	var files []string

	addRequired := func(rel string) error {
		content, err := readMarkdownFile(root, rel)
		if err != nil {
			return err
		}
		sections = append(sections, strings.TrimSpace(content))
		files = append(files, filepath.ToSlash(rel))
		return nil
	}

	addOptional := func(rel string) error {
		content, err := readMarkdownFile(root, rel)
		if err != nil {
			return nil
		}
		if strings.TrimSpace(content) == "" {
			return nil
		}
		sections = append(sections, strings.TrimSpace(content))
		files = append(files, filepath.ToSlash(rel))
		return nil
	}

	if err := addRequired("SKILL.md"); err != nil {
		return "", nil, err
	}

	for _, rel := range []string{
		"docs/structure-templates.md",
		"docs/jargon-lexicon.md",
		"docs/emotional-protocol.md",
		"docs/domain-knowledge.md",
	} {
		if err := addRequired(rel); err != nil {
			return "", nil, err
		}
	}

	libraryRel, err := selectLibraryFile(root, topic)
	if err != nil {
		return "", nil, err
	}
	if err := addRequired(libraryRel); err != nil {
		return "", nil, err
	}

	_ = addOptional("examples/input-sample.md")
	_ = addOptional("examples/output-sample.md")

	if len(sections) == 0 {
		return "", nil, fmt.Errorf("Skill 目录中未找到可用内容: %s", root)
	}

	sections = append(sections, runtimeGuardrails())

	return strings.Join(sections, "\n\n---\n\n"), files, nil
}

func runtimeGuardrails() string {
	return strings.TrimSpace(`
# 本工具运行约束

你正在口播视频工具里生成文案，不是在 Codex、Claude、Cursor 或任何 Agent 运行环境里。

必须遵守：
- 只输出中文口播稿正文。
- 输出 2-3 版，每版独立成段。
- 不要输出 Markdown 代码块。
- 不要输出 XML、DSML、JSON、YAML 或工具调用格式。
- 不要输出 <｜｜DSML｜｜tool_calls>、Bash、curl、pwd、ls、find、Invoke-WebRequest 等命令内容。
- 不要声称正在读取文件、搜索网页、调用工具、写入飞书或执行脚本。
- 遇到 skill 中要求实时搜索、写 Bash、写飞书、读写本地文件的步骤时，只把它理解为事实谨慎原则，不要执行，也不要把步骤写出来。
- 如果事实不确定，用概括表述替代，不要编造具体企业、金额、时间、地点。

最终回答只能是给用户可直接复制到短视频口播里的文案。
`)
}

func readLegacyMarkdownTree(root string) (string, []string, error) {
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return "", nil, err
	}
	info, err := os.Stat(absRoot)
	if err != nil {
		return "", nil, err
	}
	if !info.IsDir() {
		absRoot = filepath.Dir(absRoot)
	}

	var mdFiles []string
	if err := filepath.WalkDir(absRoot, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			return nil
		}
		if strings.HasSuffix(strings.ToLower(d.Name()), ".md") {
			mdFiles = append(mdFiles, path)
		}
		return nil
	}); err != nil {
		return "", nil, err
	}

	sort.Strings(mdFiles)

	var sections []string
	var files []string
	for _, path := range mdFiles {
		content, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		text := strings.TrimSpace(string(content))
		if text == "" {
			continue
		}
		sections = append(sections, text)
		rel, err := filepath.Rel(absRoot, path)
		if err != nil {
			rel = filepath.Base(path)
		}
		files = append(files, filepath.ToSlash(rel))
	}

	if len(sections) == 0 {
		return "", nil, fmt.Errorf("Skill 目录中未找到任何 .md 文件: %s", root)
	}

	return strings.Join(sections, "\n\n---\n\n"), files, nil
}

func readMarkdownFile(root, rel string) (string, error) {
	path := filepath.Join(root, filepath.FromSlash(rel))
	content, err := os.ReadFile(path)
	if err != nil {
		return "", err
	}
	return string(content), nil
}

func resolveSkillRoot(path string) (string, error) {
	candidates := make([]string, 0, len(defaultLocalSkillRoots)+1)
	if strings.TrimSpace(path) != "" {
		candidates = append(candidates, path)
	}
	candidates = append(candidates, defaultLocalSkillRoots...)

	seen := map[string]struct{}{}
	var lastErr error
	for _, candidate := range candidates {
		candidate = strings.TrimSpace(candidate)
		if candidate == "" {
			continue
		}
		if _, ok := seen[candidate]; ok {
			continue
		}
		seen[candidate] = struct{}{}

		root, err := findSkillRoot(candidate)
		if err == nil {
			return root, nil
		}
		lastErr = err
	}

	if lastErr != nil {
		return "", lastErr
	}
	return "", fmt.Errorf("本地 Skill 路径未设置")
}

func findSkillRoot(start string) (string, error) {
	abs, err := filepath.Abs(start)
	if err != nil {
		return "", err
	}

	info, err := os.Stat(abs)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		abs = filepath.Dir(abs)
	}

	for {
		if _, err := os.Stat(filepath.Join(abs, "SKILL.md")); err == nil {
			return abs, nil
		}
		parent := filepath.Dir(abs)
		if parent == abs {
			break
		}
		abs = parent
	}

	return "", fmt.Errorf("未找到 SKILL.md: %s", start)
}

type libraryRule struct {
	file     string
	keywords []string
}

var libraryRules = []libraryRule{
	{
		file: "招聘会活动.md",
		keywords: []string{
			"招聘会", "双选会", "人才对接会", "引才", "专场", "海归专场", "空中双选会", "线上双选会", "报名", "活动", "宣讲会",
		},
	},
	{
		file: "单企业直招.md",
		keywords: []string{
			"直招", "单企业", "单组织", "招聘公告", "校招", "社招", "单独招聘", "福耀", "字节", "腾讯", "阿里", "华为", "联合国",
		},
	},
	{
		file: "平台工具.md",
		keywords: []string{
			"平台", "网站", "网址", "入口", "渠道", "工具", "官网", "系统", "小程序", "国际组织",
		},
	},
	{
		file: "认知打破.md",
		keywords: []string{
			"误区", "真相", "认知", "打破", "醒醒", "别再", "其实不是", "不是", "为什么", "颠覆",
		},
	},
	{
		file: "赛道红利.md",
		keywords: []string{
			"红利", "赛道", "机会", "通道", "政策", "补贴", "安家费", "住房补贴", "专项", "绿色通道", "人才引进",
		},
	},
	{
		file: "实操攻略.md",
		keywords: []string{
			"怎么", "步骤", "教程", "方法", "timeline", "时间线", "秋招", "春招", "简历", "投递", "建档", "认证", "档案", "应届", "落户", "选调", "国考", "省考",
		},
	},
}

func selectLibraryFile(root, topic string) (string, error) {
	libraryDir := filepath.Join(root, "library")
	entries, err := os.ReadDir(libraryDir)
	if err != nil {
		return "", fmt.Errorf("读取 library 失败: %w", err)
	}

	var files []string
	scores := map[string]int{}
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasSuffix(strings.ToLower(entry.Name()), ".md") {
			continue
		}
		files = append(files, entry.Name())
		scores[entry.Name()] = 0
	}
	sort.Strings(files)

	if len(files) == 0 {
		return "", fmt.Errorf("library 中未找到任何 .md 文件: %s", libraryDir)
	}

	normalizedTopic := normalizeText(topic)
	for _, rule := range libraryRules {
		score := 0
		for _, keyword := range rule.keywords {
			needle := normalizeText(keyword)
			if needle != "" && strings.Contains(normalizedTopic, needle) {
				score++
			}
		}
		if strings.Contains(normalizedTopic, normalizeText(strings.TrimSuffix(rule.file, ".md"))) {
			score += 2
		}
		if _, ok := scores[rule.file]; ok {
			scores[rule.file] += score
		}
	}

	defaultFile := "实操攻略.md"
	if _, ok := scores[defaultFile]; !ok {
		defaultFile = files[0]
	}

	bestScore := 0
	for _, file := range files {
		if scores[file] > bestScore {
			bestScore = scores[file]
		}
	}
	if bestScore == 0 {
		return filepath.ToSlash(filepath.Join("library", defaultFile)), nil
	}

	for _, rule := range libraryRules {
		if scores[rule.file] == bestScore {
			return filepath.ToSlash(filepath.Join("library", rule.file)), nil
		}
	}

	return filepath.ToSlash(filepath.Join("library", defaultFile)), nil
}

func normalizeText(s string) string {
	s = strings.ToLower(strings.TrimSpace(s))
	replacer := strings.NewReplacer(
		" ", "",
		"\t", "",
		"\n", "",
		"\r", "",
		"，", "",
		"。", "",
		"、", "",
		"！", "",
		"？", "",
		"：", "",
		"；", "",
		"（", "",
		"）", "",
		"【", "",
		"】", "",
		"“", "",
		"”", "",
		"'", "",
		"\"", "",
	)
	return replacer.Replace(s)
}

func skillCacheDir(rawURL string) string {
	sum := sha1.Sum([]byte(strings.TrimSpace(rawURL)))
	return filepath.Join(os.TempDir(), "koubo-skill-cache-"+hex.EncodeToString(sum[:8]))
}

type ghContent struct {
	Name        string `json:"name"`
	Path        string `json:"path"`
	Type        string `json:"type"`
	DownloadURL string `json:"download_url"`
}

func readGitHub(cfg *store.Config, topic string) (string, []string, error) {
	cacheDir := skillCacheDir(cfg.Skill.GithubURL)
	now := time.Now().Unix()

	if cfg.Skill.GithubCacheUntil > now {
		if _, err := os.Stat(cacheDir); err == nil {
			prompt, files, err := buildPromptFromDir(cacheDir, topic)
			if err == nil {
				return prompt, files, nil
			}
		}
	}

	apiURL, branch := rawToAPI(cfg.Skill.GithubURL)
	if apiURL == "" {
		return "", nil, fmt.Errorf("无法解析 GitHub URL: %s", cfg.Skill.GithubURL)
	}

	client := &http.Client{Timeout: 20 * time.Second}
	if err := fetchGitHubTree(client, apiURL, branch, cacheDir); err != nil {
		return "", nil, fmt.Errorf("GitHub 同步失败: %w", err)
	}

	cfg.Skill.GithubCacheUntil = now + 24*3600
	prompt, files, err := buildPromptFromDir(cacheDir, topic)
	if err != nil {
		return "", nil, fmt.Errorf("GitHub 缓存读取失败: %w", err)
	}
	return prompt, files, nil
}

// rawToAPI 将 raw.githubusercontent.com 或 github.com/tree URL 转为 GitHub contents API URL。
func rawToAPI(rawURL string) (apiURL, branch string) {
	rawURL = strings.TrimRight(strings.TrimSpace(rawURL), "/")
	switch {
	case strings.HasPrefix(rawURL, "https://api.github.com/repos/"):
		return rawURL, ""
	case strings.HasPrefix(rawURL, "https://raw.githubusercontent.com/"):
		return rawGithubToAPI(rawURL)
	case strings.HasPrefix(rawURL, "https://github.com/"):
		return treeGithubToAPI(rawURL)
	default:
		return "", ""
	}
}

func rawGithubToAPI(rawURL string) (apiURL, branch string) {
	prefix := "https://raw.githubusercontent.com/"
	rest := strings.TrimPrefix(rawURL, prefix)
	parts := strings.SplitN(rest, "/", 3)
	if len(parts) < 3 {
		return "", ""
	}

	owner, repo := parts[0], parts[1]
	remainder := parts[2]
	idx := strings.Index(remainder, "/")
	if idx < 0 {
		return fmt.Sprintf("https://api.github.com/repos/%s/%s/contents", owner, repo), remainder
	}
	branch = remainder[:idx]
	subPath := remainder[idx+1:]
	return fmt.Sprintf("https://api.github.com/repos/%s/%s/contents/%s", owner, repo, subPath), branch
}

func treeGithubToAPI(treeURL string) (apiURL, branch string) {
	prefix := "https://github.com/"
	rest := strings.TrimPrefix(treeURL, prefix)
	parts := strings.SplitN(rest, "/", 5)
	if len(parts) < 4 || parts[2] != "tree" {
		return "", ""
	}

	owner, repo := parts[0], parts[1]
	branch = parts[3]
	if len(parts) == 4 || parts[4] == "" {
		return fmt.Sprintf("https://api.github.com/repos/%s/%s/contents", owner, repo), branch
	}
	return fmt.Sprintf("https://api.github.com/repos/%s/%s/contents/%s", owner, repo, parts[4]), branch
}

func fetchGitHubTree(client *http.Client, apiURL, branch, destDir string) error {
	url := apiURL
	if branch != "" {
		url += "?ref=" + branch
	}

	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("Accept", "application/vnd.github.v3+json")
	req.Header.Set("User-Agent", "koubo-video-tool")

	resp, err := client.Do(req)
	if err != nil {
		return fmt.Errorf("请求 GitHub API 失败: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return fmt.Errorf("GitHub API 返回状态 %d (URL: %s)", resp.StatusCode, url)
	}

	var items []ghContent
	if err := json.NewDecoder(resp.Body).Decode(&items); err != nil {
		return fmt.Errorf("解析 GitHub API 响应失败: %w", err)
	}

	os.RemoveAll(destDir)
	if err := os.MkdirAll(destDir, 0o755); err != nil {
		return err
	}

	for _, item := range items {
		switch item.Type {
		case "dir":
			subURL := fmt.Sprintf("https://api.github.com/repos/%s/contents/%s", extractRepo(apiURL), item.Path)
			subDest := filepath.Join(destDir, item.Name)
			if err := fetchGitHubTree(client, subURL, branch, subDest); err != nil {
				return err
			}

		case "file":
			if !strings.HasSuffix(strings.ToLower(item.Name), ".md") {
				continue
			}
			content, err := fetchFile(client, item.DownloadURL)
			if err != nil {
				continue
			}
			if err := os.WriteFile(filepath.Join(destDir, item.Name), content, 0o644); err != nil {
				return err
			}
		}
	}

	return nil
}

func fetchFile(client *http.Client, url string) ([]byte, error) {
	resp, err := client.Get(url)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("fetch file %s: status %d", url, resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

func extractRepo(apiURL string) string {
	prefix := "https://api.github.com/repos/"
	if !strings.HasPrefix(apiURL, prefix) {
		return ""
	}
	rest := strings.TrimPrefix(apiURL, prefix)
	parts := strings.SplitN(rest, "/", 3)
	if len(parts) >= 2 {
		return parts[0] + "/" + parts[1]
	}
	return rest
}

func readLocalDir(root string) (string, error) {
	prompt, _, err := buildFromLocal(root, "")
	if err == nil {
		return prompt, nil
	}
	legacyPrompt, _, legacyErr := readLegacyMarkdownTree(root)
	if legacyErr == nil {
		return legacyPrompt, nil
	}
	return "", err
}
