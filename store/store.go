package store

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"
)

// Config 应用设置
type Config struct {
	Chanjing struct {
		AppID     string `json:"app_id"`
		SecretKey string `json:"secret_key"`
	} `json:"chanjing"`
	LLM struct {
		APIURL string `json:"api_url"`
		APIKey string `json:"api_key"`
		Model  string `json:"model"`
		// Thinking keeps the provider's thinking mode on for the Agent's own
		// model. Off by default: it costs 10-20s per turn. The structured-output
		// specialists never use it, because thinking rejects a forced tool call.
		Thinking bool `json:"thinking"`
	} `json:"llm"`
	Search struct {
		Provider string `json:"provider"` // "local" | "tavily" | "brave"
		APIKey   string `json:"api_key"`
	} `json:"search"`
	Skill struct {
		Source           string `json:"source"` // "local" | "github"
		LocalPath        string `json:"local_path"`
		GithubURL        string `json:"github_url"`
		GithubCacheUntil int64  `json:"github_cache_until"`
	} `json:"skill"`
	// Agent 是 Python Agent 服务所需的模型设置。它与上面的设置共用同一个
	// config.json，因此用户只填一处，密钥也只有一套加密方式。
	Agent AgentConfig `json:"agent"`
}

// Task 视频生成任务
type Task struct {
	TaskID      string `json:"task_id"`
	Status      string `json:"status"` // pending|generating|done|failed|timeout
	Progress    int    `json:"progress"`
	VideoURL    string `json:"video_url"`
	Error       string `json:"error"`
	Script      string `json:"script,omitempty"`
	Params      any    `json:"params"`
	CreatedAt   string `json:"created_at"`
	CompletedAt string `json:"completed_at"`
	PollCount   int    `json:"poll_count"`
}

// Avatar 数字人形象
type Avatar struct {
	ID         string `json:"id"`
	Name       string `json:"name"`
	PreviewURL string `json:"preview_url"`
}

// Voice 人声
type Voice struct {
	ID              string `json:"id"`
	Name            string `json:"name"`
	Gender          string `json:"gender"`
	Description     string `json:"description"`
	PreviewAudioURL string `json:"preview_audio_url"`
	AudioSource     int    `json:"audio_source,omitempty"`
}

// SavedTopic 是搜索结果加入选题库后的本地记录。RawInfo 保留经过核验的
// 结构化招聘事实，后续口播生成可以直接使用它，不必再次联网搜索。
type SavedTopic struct {
	Title     string `json:"title"`
	Type      string `json:"type"`
	SourceURL string `json:"source_url"`
	RawInfo   any    `json:"raw_info"`
	CreatedAt string `json:"created_at"`
	Status    string `json:"status"`
}

// MaskedValue 用于前端传输时隐藏密钥的真实值
const MaskedValue = "\x00masked\x00"

var mu sync.RWMutex

var (
	ErrTaskNotFound   = errors.New("任务不存在")
	ErrTaskInProgress = errors.New("生成中的任务不能删除")
)

func read(path string, v any) error {
	mu.RLock()
	defer mu.RUnlock()
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return json.NewDecoder(f).Decode(v)
}

func write(path string, v any) error {
	mu.Lock()
	defer mu.Unlock()
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	return enc.Encode(v)
}

func LoadConfig(path string) (Config, error) {
	var c Config
	if err := read(path, &c); err != nil {
		return c, err
	}
	// 解密敏感字段（旧明文无 "ENC:" 前缀则直接返回，下次保存自动加密）。
	// Agent 段的两个密钥由同一个方法一起处理，避免两处各解一次。
	plain := c.decrypted()
	if c.Chanjing.SecretKey != "" && plain.Chanjing.SecretKey == "" {
		return Config{}, fmt.Errorf("解密蝉镜 SecretKey 失败")
	}
	if c.LLM.APIKey != "" && plain.LLM.APIKey == "" {
		return Config{}, fmt.Errorf("解密 LLM API Key 失败")
	}
	if c.Search.APIKey != "" && plain.Search.APIKey == "" {
		return Config{}, fmt.Errorf("解密搜索 API Key 失败")
	}
	return plain, nil
}

func SaveConfig(path string, c Config) error {
	// 加密敏感字段后再写盘（含 Agent 段的模型密钥）
	return write(path, c.encrypted())
}

func LoadTasks(path string) ([]Task, error) {
	var tasks []Task
	if err := read(path, &tasks); err != nil {
		if os.IsNotExist(err) {
			return []Task{}, nil
		}
		return nil, err
	}
	return tasks, nil
}

func SaveTasks(path string, tasks []Task) error {
	return write(path, tasks)
}

// PruneExpiredTasks removes terminal records older than cutoff and keeps active jobs.
func PruneExpiredTasks(path string, cutoff time.Time) (int, error) {
	mu.Lock()
	defer mu.Unlock()

	tasks, err := loadTasksUnlocked(path)
	if err != nil {
		return 0, err
	}
	kept := make([]Task, 0, len(tasks))
	removed := 0
	for _, task := range tasks {
		createdAt, parseErr := time.Parse(time.RFC3339, task.CreatedAt)
		if isActiveTask(task) || parseErr != nil || !createdAt.Before(cutoff) {
			kept = append(kept, task)
			continue
		}
		removed++
	}
	if removed == 0 {
		return 0, nil
	}
	return removed, saveTasksUnlocked(path, kept)
}

func DeleteTask(path, taskID string) error {
	mu.Lock()
	defer mu.Unlock()

	tasks, err := loadTasksUnlocked(path)
	if err != nil {
		return err
	}
	kept := make([]Task, 0, len(tasks))
	found := false
	for _, task := range tasks {
		if task.TaskID != taskID {
			kept = append(kept, task)
			continue
		}
		found = true
		if isActiveTask(task) {
			return ErrTaskInProgress
		}
	}
	if !found {
		return ErrTaskNotFound
	}
	return saveTasksUnlocked(path, kept)
}

// SaveTask inserts or replaces one task record under the same lock the rest of
// the task file uses, so a refresh and a submission cannot interleave writes.
func SaveTask(path string, task Task) error {
	mu.Lock()
	defer mu.Unlock()

	tasks, err := loadTasksUnlocked(path)
	if err != nil {
		return err
	}
	replaced := false
	for index := range tasks {
		if tasks[index].TaskID != task.TaskID {
			continue
		}
		if tasks[index].CreatedAt == "" {
			task.CreatedAt = tasks[index].CreatedAt
		}
		tasks[index] = task
		replaced = true
		break
	}
	if !replaced {
		tasks = append(tasks, task)
	}
	return saveTasksUnlocked(path, tasks)
}

// ClearTerminalTasks clears history without hiding tasks that are still running.
func ClearTerminalTasks(path string) (int, error) {
	mu.Lock()
	defer mu.Unlock()

	tasks, err := loadTasksUnlocked(path)
	if err != nil {
		return 0, err
	}
	kept := make([]Task, 0, len(tasks))
	for _, task := range tasks {
		if isActiveTask(task) {
			kept = append(kept, task)
		}
	}
	removed := len(tasks) - len(kept)
	if removed == 0 {
		return 0, nil
	}
	return removed, saveTasksUnlocked(path, kept)
}

func isActiveTask(task Task) bool {
	return task.Status == "pending" || task.Status == "generating"
}

func loadTasksUnlocked(path string) ([]Task, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []Task{}, nil
		}
		return nil, err
	}
	defer f.Close()
	var tasks []Task
	if err := json.NewDecoder(f).Decode(&tasks); err != nil {
		return nil, err
	}
	return tasks, nil
}

func saveTasksUnlocked(path string, tasks []Task) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	return enc.Encode(tasks)
}

// UpdateTask 原子地读取、修改、写回单个任务，避免并发覆盖
func UpdateTask(path string, taskID string, fn func(*Task)) error {
	mu.Lock()
	defer mu.Unlock()

	var tasks []Task
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	if err := json.NewDecoder(f).Decode(&tasks); err != nil {
		f.Close()
		return err
	}
	f.Close()

	found := false
	for i := range tasks {
		if tasks[i].TaskID == taskID {
			fn(&tasks[i])
			found = true
			break
		}
	}
	if !found {
		return nil // 任务可能已被删除，静默跳过
	}

	out, err := os.Create(path)
	if err != nil {
		return err
	}
	defer out.Close()
	enc := json.NewEncoder(out)
	enc.SetIndent("", "  ")
	return enc.Encode(tasks)
}

func LoadAvatars(path string) ([]Avatar, error) {
	var a []Avatar
	if err := read(path, &a); err != nil {
		if os.IsNotExist(err) {
			return []Avatar{}, nil
		}
		return nil, err
	}
	return a, nil
}

func SaveAvatars(path string, avatars []Avatar) error {
	return write(path, avatars)
}

func LoadVoices(path string) ([]Voice, error) {
	var v []Voice
	if err := read(path, &v); err != nil {
		if os.IsNotExist(err) {
			return []Voice{}, nil
		}
		return nil, err
	}
	return v, nil
}

func LoadTopics(path string) ([]SavedTopic, error) {
	var topics []SavedTopic
	if err := read(path, &topics); err != nil {
		if os.IsNotExist(err) {
			return []SavedTopic{}, nil
		}
		return nil, err
	}
	return topics, nil
}

func SaveTopics(path string, topics []SavedTopic) error {
	return write(path, topics)
}

// AddTopic 保存一条选题，并按原文 URL 去重，避免重复点击造成重复记录。
func AddTopic(path string, topic SavedTopic) (SavedTopic, bool, error) {
	mu.Lock()
	defer mu.Unlock()

	topics, err := loadTopicsUnlocked(path)
	if err != nil {
		return SavedTopic{}, false, err
	}
	for _, existing := range topics {
		if topic.SourceURL != "" && existing.SourceURL == topic.SourceURL {
			return existing, false, nil
		}
	}
	topics = append(topics, topic)
	if err := saveTopicsUnlocked(path, topics); err != nil {
		return SavedTopic{}, false, err
	}
	return topic, true, nil
}

// DeleteTopic removes a saved topic by its source URL. SourceURL is the
// stable identifier used by AddTopic for deduplication.
func DeleteTopic(path, sourceURL string) (bool, error) {
	mu.Lock()
	defer mu.Unlock()

	topics, err := loadTopicsUnlocked(path)
	if err != nil {
		return false, err
	}
	for i, topic := range topics {
		if topic.SourceURL != sourceURL {
			continue
		}
		topics = append(topics[:i], topics[i+1:]...)
		if err := saveTopicsUnlocked(path, topics); err != nil {
			return false, err
		}
		return true, nil
	}
	return false, nil
}

func loadTopicsUnlocked(path string) ([]SavedTopic, error) {
	f, err := os.Open(path)
	if err != nil {
		if os.IsNotExist(err) {
			return []SavedTopic{}, nil
		}
		return nil, err
	}
	defer f.Close()
	var topics []SavedTopic
	if err := json.NewDecoder(f).Decode(&topics); err != nil {
		return nil, err
	}
	return topics, nil
}

func saveTopicsUnlocked(path string, topics []SavedTopic) error {
	f, err := os.Create(path)
	if err != nil {
		return err
	}
	defer f.Close()
	enc := json.NewEncoder(f)
	enc.SetIndent("", "  ")
	return enc.Encode(topics)
}
