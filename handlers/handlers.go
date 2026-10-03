package handlers

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"koubo-video-tool/chanjing"
	"koubo-video-tool/internal/agentproxy"
	"koubo-video-tool/llm"
	"koubo-video-tool/scheduler"
	"koubo-video-tool/skill"
	"koubo-video-tool/store"
	"koubo-video-tool/topicsearch"
)

type Handler struct {
	DataDir               string
	Store                 *StorePaths
	Chanjing              chanjing.ClientInterface
	Scheduler             *scheduler.Scheduler
	AgentProxy            *agentproxy.Client
	TopicSearch           TopicSearchFunc
	TopicSearchWithConfig TopicSearchConfigFunc
	// Mode is the application this deployment serves. Shared pages read it to
	// send the user back to the right home.
	Mode AppMode
}

// TopicSearchFunc is injectable for tests and keeps the HTTP layer independent
// from the concrete web-search provider.
type TopicSearchFunc func(ctx context.Context, query, apiURL, apiKey, model string) (topicsearch.Response, error)

// TopicSearchConfigFunc is the production path. It passes the saved search
// provider credentials to the Agent without coupling tests to config storage.
type TopicSearchConfigFunc func(ctx context.Context, query string, cfg store.Config) (topicsearch.Response, error)

type StorePaths struct {
	Config  string
	Tasks   string
	Avatars string
	Voices  string
	Topics  string
}

func New(dataDir string, cj chanjing.ClientInterface, sch *scheduler.Scheduler) *Handler {
	searchService := topicsearch.NewService()
	agentProxy, proxyErr := agentproxy.NewFromEnv()
	if proxyErr != nil {
		log.Printf("agent proxy disabled: %v", proxyErr)
	}
	return &Handler{
		DataDir:    dataDir,
		Chanjing:   cj,
		Scheduler:  sch,
		AgentProxy: agentProxy,
		Store: &StorePaths{
			Config:  dataDir + "/config.json",
			Tasks:   dataDir + "/tasks.json",
			Avatars: dataDir + "/avatars.json",
			Voices:  dataDir + "/voices.json",
			Topics:  dataDir + "/topics.json",
		},
		TopicSearchWithConfig: searchService.SearchWithConfigFromStore,
	}
}

func (h *Handler) Register() {
	h.RegisterOn(http.DefaultServeMux)
}

// AppMode selects which application a deployment serves. The Agent app and the
// classic video tool share capability code, but they are separate products with
// no overlapping surface: a build serves one of them, not both.
type AppMode string

const (
	// ModeAll keeps both surfaces, which is what the pre-split binary served.
	ModeAll AppMode = ""
	// ModeAgent serves the conversation, its settings and the capability
	// endpoints the Python Agent calls.
	ModeAgent AppMode = "agent"
	// ModeTool serves the classic tool: avatars, voices, tasks, topic search.
	ModeTool AppMode = "tool"
)

// RegisterOn makes route registration testable without mutating the process
// global mux. The production entry point continues to use Register().
func (h *Handler) RegisterOn(mux *http.ServeMux) {
	h.RegisterMode(mux, ModeAll)
}

// RegisterMode registers only the routes one application owns. Settings stay in
// both: each app has its own settings file, and the Agent needs the digital
// human credentials for its own submission step.
func (h *Handler) RegisterMode(mux *http.ServeMux, mode AppMode) {
	h.Mode = mode
	mux.HandleFunc("/api/app", h.handleAppInfo)
	// Lists and task submission belong to the digital-human feature, which both
	// applications now use: the Agent submits a video through exactly the same
	// entry point the classic tool's own button uses.
	mux.HandleFunc("/api/avatars", h.handleAvatars)
	mux.HandleFunc("/api/voices", h.handleVoices)
	mux.HandleFunc("/api/tasks", h.handleTasks)
	mux.HandleFunc("/api/tasks/", h.handleTaskByID)
	if mode != ModeAgent {
		mux.HandleFunc("/api/scripts/generate", h.handleGenerateScript)
		mux.HandleFunc("/api/topic-search", h.handleTopicSearch)
		mux.HandleFunc("/api/topics", h.handleTopics)
	}
	mux.HandleFunc("/api/settings", h.handleSettings)
	if mode != ModeTool {
		mux.HandleFunc("/api/agent/chat", h.handleAgentChat)
		mux.HandleFunc("/api/agent/chat/stream", h.handleAgentChatStream)
		mux.HandleFunc("/api/agent/session/", h.handleAgentSession)
		mux.HandleFunc("/api/agent/config", h.handleAgentConfig)
		mux.HandleFunc("/internal/tools/search-platform", h.handleInternalSearchPlatform)
		mux.HandleFunc("/internal/tools/fetch-source", h.handleInternalFetchSource)
		mux.HandleFunc("/internal/tools/generate-digital-human", h.handleInternalGenerateDigitalHuman)
		mux.HandleFunc("/internal/tools/generation/", h.handleInternalGenerationStatus)
	}
}

// GET /api/app
//
// Which application this deployment is. The settings fragment is shared by both
// apps, so it asks here instead of guessing which home to return to — guessing
// is what sent the Agent's settings page to the video tool's /main.html, which
// that app does not serve.
func (h *Handler) handleAppInfo(w http.ResponseWriter, r *http.Request) {
	mode := string(h.Mode)
	if mode == "" {
		mode = "all"
	}
	writeJSON(w, map[string]string{"mode": mode})
}

type agentErrorResponse struct {
	ErrorCode string         `json:"error_code"`
	Message   string         `json:"message"`
	Retryable bool           `json:"retryable"`
	RequestID string         `json:"request_id"`
	Details   map[string]any `json:"details"`
}

func writeAgentError(w http.ResponseWriter, status int, code, message string, retryable bool, requestID string, details map[string]any) {
	if details == nil {
		details = map[string]any{}
	}
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(agentErrorResponse{
		ErrorCode: code,
		Message:   message,
		Retryable: retryable,
		RequestID: requestID,
		Details:   details,
	})
}

type internalSearchRequest struct {
	RequestID  string `json:"request_id"`
	Query      string `json:"query"`
	MaxResults int    `json:"max_results"`
}

type internalSearchResponse struct {
	RequestID           string               `json:"request_id"`
	Query               string               `json:"query"`
	TotalFound          int                  `json:"total_found"`
	Results             []topicsearch.Result `json:"results"`
	AIProcessed         bool                 `json:"ai_processed"`
	SearchProvider      string               `json:"search_provider"`
	SearchExecuted      bool                 `json:"search_executed"`
	WebSearchCallCount  int                  `json:"web_search_call_count"`
	RawResultCount      int                  `json:"raw_result_count"`
	AcceptedResultCount int                  `json:"accepted_result_count"`
	FilteredResultCount int                  `json:"filtered_result_count"`
}

type internalFetchSourceRequest struct {
	RequestID string `json:"request_id"`
	SourceURL string `json:"source_url"`
	MaxChars  int    `json:"max_chars"`
}

type internalFetchSourceResponse struct {
	RequestID   string `json:"request_id"`
	SourceURL   string `json:"source_url"`
	FinalURL    string `json:"final_url"`
	Title       string `json:"title"`
	Content     string `json:"content"`
	PublishedAt string `json:"published_at"`
	FetchedAt   string `json:"fetched_at"`
}

type internalDigitalHumanParams struct {
	AvatarID            string  `json:"avatar_id"`
	AudioManID          string  `json:"audio_man_id"`
	Speed               float64 `json:"speed"`
	Volume              int     `json:"volume"`
	BgColor             string  `json:"bg_color"`
	Resolution          string  `json:"resolution"`
	SubtitleEnabled     bool    `json:"subtitle_enabled"`
	FigureType          string  `json:"figure_type"`
	DriveMode           string  `json:"drive_mode"`
	Backway             int     `json:"backway"`
	Language            string  `json:"language"`
	ComplianceWatermark bool    `json:"compliance_watermark"`
}

type internalDigitalHumanRequest struct {
	RequestID       string                     `json:"request_id"`
	TaskID          string                     `json:"task_id"`
	DraftID         string                     `json:"draft_id"`
	DraftVersion    int                        `json:"draft_version"`
	AuthorizationID string                     `json:"authorization_id"`
	IdempotencyKey  string                     `json:"idempotency_key"`
	GenerationID    string                     `json:"generation_id"`
	Script          string                     `json:"script"`
	Params          internalDigitalHumanParams `json:"params"`
}

type internalDigitalHumanResponse struct {
	RequestID      string `json:"request_id"`
	GenerationID   string `json:"generation_id"`
	ProviderTaskID string `json:"provider_task_id"`
	Status         string `json:"status"`
	CreatedAt      string `json:"created_at"`
}

// internalGenerationStatusResponse is the read-only view of one generation.
//
// ``status`` is the Agent-facing vocabulary (pending / processing / completed /
// failed); ``raw_status`` keeps the local task state so a caller can tell an
// unknown state from a slow one.
type internalGenerationStatusResponse struct {
	RequestID      string `json:"request_id"`
	ProviderTaskID string `json:"provider_task_id"`
	Status         string `json:"status"`
	RawStatus      string `json:"raw_status"`
	Progress       int    `json:"progress"`
	VideoURL       string `json:"video_url"`
	Error          string `json:"error"`
	CreatedAt      string `json:"created_at"`
	CompletedAt    string `json:"completed_at"`
	Failed         bool   `json:"failed"`
}

func newInternalSearchResponse(requestID string, result topicsearch.Response) internalSearchResponse {
	if result.Results == nil {
		result.Results = []topicsearch.Result{}
	}
	return internalSearchResponse{
		RequestID:           requestID,
		Query:               result.Query,
		TotalFound:          result.TotalFound,
		Results:             result.Results,
		AIProcessed:         result.AIProcessed,
		SearchProvider:      result.SearchProvider,
		SearchExecuted:      result.SearchExecuted,
		WebSearchCallCount:  result.WebSearchCallCount,
		RawResultCount:      result.RawResultCount,
		AcceptedResultCount: result.AcceptedResultCount,
		FilteredResultCount: result.FilteredResultCount,
	}
}

// POST /internal/tools/search-platform
// This is the Python Agent capability boundary. It reuses the existing Go
// search service and only adapts the request/response/error contract; topic
// recommendation remains outside this handler.
func (h *Handler) handleInternalSearchPlatform(w http.ResponseWriter, r *http.Request) {
	headerRequestID := strings.TrimSpace(r.Header.Get("X-Request-ID"))
	if r.Method != http.MethodPost {
		requestID := headerRequestID
		if requestID == "" {
			requestID = requestIDFor(r)
		}
		writeAgentError(w, http.StatusMethodNotAllowed, "INVALID_REQUEST", "仅支持 POST", false, requestID, nil)
		return
	}

	var request internalSearchRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 32<<10)).Decode(&request); err != nil {
		requestID := headerRequestID
		if requestID == "" {
			requestID = requestIDFor(r)
		}
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "请求格式错误", false, requestID, nil)
		return
	}

	bodyRequestID := strings.TrimSpace(request.RequestID)
	if headerRequestID != "" && bodyRequestID != "" && headerRequestID != bodyRequestID {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "request_id 与 X-Request-ID 不一致", false, headerRequestID, nil)
		return
	}
	requestID := headerRequestID
	if requestID == "" {
		requestID = bodyRequestID
	}
	if requestID == "" {
		requestID = requestIDFor(r)
	}

	request.Query = strings.TrimSpace(request.Query)
	if request.Query == "" {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "query 不能为空", false, requestID, map[string]any{"field": "query"})
		return
	}
	if request.MaxResults == 0 {
		request.MaxResults = 10
	}
	if request.MaxResults < 1 || request.MaxResults > 15 {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "max_results 必须在 1 到 15 之间", false, requestID, map[string]any{"field": "max_results"})
		return
	}

	cfg, err := store.LoadConfig(h.Store.Config)
	if err != nil {
		writeAgentError(w, http.StatusInternalServerError, "INTERNAL_ERROR", "读取搜索配置失败", false, requestID, nil)
		return
	}
	if strings.TrimSpace(cfg.LLM.APIURL) == "" || strings.TrimSpace(cfg.LLM.APIKey) == "" {
		writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "搜索模型尚未配置", false, requestID, map[string]any{"configured": false})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 300*time.Second)
	defer cancel()
	var result topicsearch.Response
	if h.TopicSearchWithConfig != nil {
		result, err = h.TopicSearchWithConfig(ctx, request.Query, cfg)
	} else {
		result, err = topicsearch.NewService().SearchWithConfigFromStore(ctx, request.Query, cfg)
	}
	if err != nil {
		log.Printf("internal platform search failed request_id=%s query=%q: %v", requestID, request.Query, err)
		if errors.Is(err, context.DeadlineExceeded) || strings.Contains(strings.ToLower(err.Error()), "context deadline exceeded") {
			writeAgentError(w, http.StatusGatewayTimeout, "CAPABILITY_TIMEOUT", "搜索能力请求超时", true, requestID, nil)
			return
		}
		writeAgentError(w, http.StatusBadGateway, "UPSTREAM_ERROR", "搜索能力返回错误", true, requestID, nil)
		return
	}

	// The existing service caps results at 15. Preserve its metadata and cap
	// only the returned collection when the caller requests a smaller batch.
	if len(result.Results) > request.MaxResults {
		result.Results = result.Results[:request.MaxResults]
	}
	writeJSON(w, newInternalSearchResponse(requestID, result))
}

// POST /internal/tools/fetch-source
// This is the Research Tool boundary. It exposes bounded HTML fetching and
// keeps source transport failures distinct from the future Research Agent.
func (h *Handler) handleInternalFetchSource(w http.ResponseWriter, r *http.Request) {
	headerRequestID := strings.TrimSpace(r.Header.Get("X-Request-ID"))
	if r.Method != http.MethodPost {
		requestID := headerRequestID
		if requestID == "" {
			requestID = requestIDFor(r)
		}
		writeAgentError(w, http.StatusMethodNotAllowed, "INVALID_REQUEST", "仅支持 POST", false, requestID, nil)
		return
	}

	var request internalFetchSourceRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 32<<10)).Decode(&request); err != nil {
		requestID := headerRequestID
		if requestID == "" {
			requestID = requestIDFor(r)
		}
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "请求格式错误", false, requestID, nil)
		return
	}

	bodyRequestID := strings.TrimSpace(request.RequestID)
	if headerRequestID != "" && bodyRequestID != "" && headerRequestID != bodyRequestID {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "request_id 与 X-Request-ID 不一致", false, headerRequestID, nil)
		return
	}
	requestID := headerRequestID
	if requestID == "" {
		requestID = bodyRequestID
	}
	if requestID == "" {
		requestID = requestIDFor(r)
	}

	request.SourceURL = strings.TrimSpace(request.SourceURL)
	if request.SourceURL == "" {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "source_url 不能为空", false, requestID, map[string]any{"field": "source_url"})
		return
	}
	if request.MaxChars == 0 {
		request.MaxChars = 6000
	}
	if request.MaxChars < 1 || request.MaxChars > 12000 {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "max_chars 必须在 1 到 12000 之间", false, requestID, map[string]any{"field": "max_chars"})
		return
	}

	ctx, cancel := context.WithTimeout(r.Context(), 5*time.Second)
	defer cancel()
	result, err := topicsearch.NewService().FetchSource(ctx, request.SourceURL, request.MaxChars)
	if err != nil {
		var fetchErr *topicsearch.SourceFetchError
		if errors.As(err, &fetchErr) {
			details := map[string]any{}
			if fetchErr.StatusCode != 0 {
				details["upstream_status"] = fetchErr.StatusCode
			}
			switch fetchErr.Kind {
			case "invalid_request":
				writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "source fetch 请求无效", false, requestID, details)
			case "not_found":
				writeAgentError(w, http.StatusNotFound, "CAPABILITY_NOT_FOUND", "source 不存在", false, requestID, details)
			case "blocked":
				writeAgentError(w, http.StatusForbidden, "CAPABILITY_BLOCKED", "source 访问被阻止", false, requestID, details)
			case "timeout":
				writeAgentError(w, http.StatusGatewayTimeout, "CAPABILITY_TIMEOUT", "source fetch 超时", true, requestID, details)
			case "unavailable":
				writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "source fetch 能力暂不可用", true, requestID, details)
			default:
				writeAgentError(w, http.StatusBadGateway, "UPSTREAM_ERROR", "source fetch 上游返回错误", true, requestID, details)
			}
			return
		}
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
			writeAgentError(w, http.StatusGatewayTimeout, "CAPABILITY_TIMEOUT", "source fetch 超时", true, requestID, nil)
			return
		}
		writeAgentError(w, http.StatusBadGateway, "UPSTREAM_ERROR", "source fetch 返回错误", true, requestID, nil)
		return
	}

	writeJSON(w, internalFetchSourceResponse{
		RequestID:   requestID,
		SourceURL:   result.SourceURL,
		FinalURL:    result.FinalURL,
		Title:       result.Title,
		Content:     result.Content,
		PublishedAt: result.PublishedAt,
		FetchedAt:   result.FetchedAt,
	})
}

// POST /internal/tools/generate-digital-human
// This endpoint is deliberately a thin capability adapter.  Authorization,
// draft/version validation, generation records, and idempotency are owned by
// the Python Agent; Go only translates the stable request into Chanjing's
// existing CreateVideoRequest and returns the provider task id.
func (h *Handler) handleInternalGenerateDigitalHuman(w http.ResponseWriter, r *http.Request) {
	headerRequestID := strings.TrimSpace(r.Header.Get("X-Request-ID"))
	if r.Method != http.MethodPost {
		requestID := headerRequestID
		if requestID == "" {
			requestID = requestIDFor(r)
		}
		writeAgentError(w, http.StatusMethodNotAllowed, "INVALID_REQUEST", "仅支持 POST", false, requestID, nil)
		return
	}

	var request internalDigitalHumanRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&request); err != nil {
		requestID := headerRequestID
		if requestID == "" {
			requestID = requestIDFor(r)
		}
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "请求格式错误", false, requestID, nil)
		return
	}

	bodyRequestID := strings.TrimSpace(request.RequestID)
	if headerRequestID != "" && bodyRequestID != "" && headerRequestID != bodyRequestID {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "request_id 与 X-Request-ID 不一致", false, headerRequestID, nil)
		return
	}
	requestID := headerRequestID
	if requestID == "" {
		requestID = bodyRequestID
	}
	if requestID == "" {
		requestID = requestIDFor(r)
	}

	if strings.TrimSpace(request.TaskID) == "" ||
		strings.TrimSpace(request.DraftID) == "" ||
		request.DraftVersion < 1 ||
		strings.TrimSpace(request.AuthorizationID) == "" ||
		strings.TrimSpace(request.IdempotencyKey) == "" ||
		strings.TrimSpace(request.GenerationID) == "" {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "generation 标识字段不完整", false, requestID, nil)
		return
	}
	if strings.TrimSpace(request.Script) == "" {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "script 不能为空", false, requestID, map[string]any{"field": "script"})
		return
	}
	if strings.TrimSpace(request.Params.AvatarID) == "" || strings.TrimSpace(request.Params.AudioManID) == "" {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "params.avatar_id 和 params.audio_man_id 不能为空", false, requestID, nil)
		return
	}

	params := request.Params
	if params.Speed == 0 {
		params.Speed = 1.0
	}
	if params.Volume == 0 {
		params.Volume = 100
	}
	if params.FigureType == "" {
		params.FigureType = "stand_body"
	}
	if params.DriveMode == "" {
		params.DriveMode = "random"
	}
	if params.Backway == 0 {
		params.Backway = 1
	}
	if params.Language == "" {
		params.Language = "cn"
	}
	if params.BgColor == "" {
		params.BgColor = "#ffffff"
	}

	audioSource, err := h.voiceAudioSource(params.AudioManID)
	if err != nil {
		writeAgentError(w, http.StatusInternalServerError, "INTERNAL_ERROR", "读取人声配置失败", false, requestID, nil)
		return
	}
	screenWidth, screenHeight := 1080, 1920
	if params.Resolution == "720P" {
		screenWidth, screenHeight = 720, 1280
	}
	cjRequest := chanjing.CreateVideoRequest{
		Person: chanjing.PersonConfig{
			ID:         params.AvatarID,
			X:          0,
			Y:          0,
			Width:      screenWidth,
			Height:     screenHeight,
			FigureType: params.FigureType,
			DriveMode:  params.DriveMode,
			Backway:    params.Backway,
		},
		Audio: chanjing.AudioConfig{
			TTS: chanjing.TTSConfig{
				Text:     []string{request.Script},
				Speed:    params.Speed,
				AudioMan: params.AudioManID,
			},
			Type:     "tts",
			Volume:   params.Volume,
			Language: params.Language,
		},
		BgColor:                params.BgColor,
		ScreenWidth:            screenWidth,
		ScreenHeight:           screenHeight,
		SubtitleConfig:         chanjing.SubtitleConfig{Show: params.SubtitleEnabled},
		AddComplianceWatermark: params.ComplianceWatermark,
		Source:                 1,
		AudioSource:            audioSource,
	}

	if h.Chanjing == nil {
		writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "数字人能力暂不可用", true, requestID, nil)
		return
	}
	token, err := h.Chanjing.GetToken()
	if err != nil {
		writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "数字人认证失败", true, requestID, nil)
		return
	}
	providerTaskID, err := h.Chanjing.CreateVideo(token, cjRequest)
	if err != nil {
		lowerError := strings.ToLower(err.Error())
		if strings.Contains(lowerError, "timeout") || strings.Contains(lowerError, "deadline") {
			writeAgentError(w, http.StatusGatewayTimeout, "CAPABILITY_TIMEOUT", "数字人提交超时", true, requestID, map[string]any{"unknown_submission": true})
			return
		}
		if strings.Contains(lowerError, "create video failed") {
			writeAgentError(w, http.StatusUnprocessableEntity, "CAPABILITY_REJECTED", "数字人 provider 拒绝提交", false, requestID, nil)
			return
		}
		writeAgentError(w, http.StatusBadGateway, "UPSTREAM_ERROR", "数字人 provider 提交失败", true, requestID, map[string]any{"unknown_submission": true})
		return
	}
	if strings.TrimSpace(providerTaskID) == "" {
		writeAgentError(w, http.StatusBadGateway, "UPSTREAM_ERROR", "数字人 provider 未返回任务 ID", true, requestID, map[string]any{"unknown_submission": true})
		return
	}

	// The provider reports progress against its own task id.  Keeping a local
	// record under that same id is what lets the existing scheduler -- and the
	// generation status adapter below -- read the video URL back later.  A
	// failure to record it must not turn a successful submission into an error.
	h.recordSubmittedGeneration(request, providerTaskID, params)

	writeJSON(w, internalDigitalHumanResponse{
		RequestID:      requestID,
		GenerationID:   request.GenerationID,
		ProviderTaskID: providerTaskID,
		Status:         "pending",
		CreatedAt:      time.Now().UTC().Format(time.RFC3339),
	})
}

// recordSubmittedGeneration stores one submitted generation as a local task.
//
// The task id is the provider task id on purpose: polling, refresh, and the
// status adapter all address a generation by the id the provider knows, so no
// second mapping table has to stay in step.
func (h *Handler) recordSubmittedGeneration(request internalDigitalHumanRequest, providerTaskID string, params internalDigitalHumanParams) {
	if h.Store == nil || h.Store.Tasks == "" {
		return
	}
	record := store.Task{
		TaskID:    providerTaskID,
		Status:    "generating",
		Progress:  0,
		Script:    request.Script,
		Params:    params,
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
	}
	if err := store.SaveTask(h.Store.Tasks, record); err != nil {
		log.Printf("record submitted generation %s failed: %v", providerTaskID, err)
	}
}

// GET /internal/tools/generation/{provider_task_id}
//
// Reports one generation's provider progress to the Agent service, which uses
// it to fill the "digital human result" panel.  Go maps the provider's numeric
// status onto a stable vocabulary; the raw numbers stay on this side.
func (h *Handler) handleInternalGenerationStatus(w http.ResponseWriter, r *http.Request) {
	headerRequestID := strings.TrimSpace(r.Header.Get("X-Request-ID"))
	requestID := headerRequestID
	if requestID == "" {
		requestID = requestIDFor(r)
	}
	if r.Method != http.MethodGet {
		writeAgentError(w, http.StatusMethodNotAllowed, "INVALID_REQUEST", "仅支持 GET", false, requestID, nil)
		return
	}

	providerTaskID := strings.TrimSpace(strings.TrimPrefix(r.URL.Path, "/internal/tools/generation/"))
	if providerTaskID == "" {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "provider_task_id 不能为空", false, requestID, nil)
		return
	}

	task, found, err := loadTaskByID(h, providerTaskID)
	if err != nil {
		writeAgentError(w, http.StatusInternalServerError, "INTERNAL_ERROR", "读取生成记录失败", false, requestID, nil)
		return
	}
	if !found {
		writeAgentError(w, http.StatusNotFound, "GENERATION_NOT_FOUND", "找不到该生成记录", false, requestID, map[string]any{
			"provider_task_id": providerTaskID,
		})
		return
	}

	// A refresh is best effort: without provider credentials the local record
	// is still worth reporting, so a failed refresh is not a failed request.
	if r.URL.Query().Get("refresh") == "1" && h.Scheduler != nil {
		if refreshed, refreshErr := h.Scheduler.Refresh(providerTaskID); refreshErr == nil {
			if saveErr := store.SaveTask(h.Store.Tasks, refreshed); saveErr != nil {
				log.Printf("persist refreshed generation %s failed: %v", providerTaskID, saveErr)
			}
			task = refreshed
		} else {
			log.Printf("refresh generation %s failed: %v", providerTaskID, refreshErr)
		}
	}

	normalized, failed := normalizeGenerationStatus(task.Status)
	writeJSON(w, internalGenerationStatusResponse{
		RequestID:      requestID,
		ProviderTaskID: task.TaskID,
		Status:         normalized,
		RawStatus:      task.Status,
		Progress:       task.Progress,
		VideoURL:       task.VideoURL,
		Error:          task.Error,
		CreatedAt:      task.CreatedAt,
		CompletedAt:    task.CompletedAt,
		Failed:         failed,
	})
}

// loadTaskByID reads one task record, reporting whether it exists.
func loadTaskByID(h *Handler, taskID string) (store.Task, bool, error) {
	if h.Store == nil || h.Store.Tasks == "" {
		return store.Task{}, false, nil
	}
	tasks, err := store.LoadTasks(h.Store.Tasks)
	if err != nil {
		return store.Task{}, false, err
	}
	for _, task := range tasks {
		if task.TaskID == taskID {
			return task, true, nil
		}
	}
	return store.Task{}, false, nil
}

// normalizeGenerationStatus maps local task states onto the Agent vocabulary.
func normalizeGenerationStatus(status string) (string, bool) {
	switch status {
	case "done":
		return "completed", false
	case "failed", "timeout":
		return "failed", true
	case "generating":
		return "processing", false
	default:
		return "pending", false
	}
}

// decodeAgentChatRequest applies the request admission rules shared by both
// chat endpoints.
//
// The blocking and streaming routes must accept exactly the same input, so the
// rules live in one place; duplicating them would let the two drift apart the
// first time one of them gains a field.  On failure it writes the error
// response itself and reports ok == false.
func (h *Handler) decodeAgentChatRequest(w http.ResponseWriter, r *http.Request) (agentproxy.ChatRequest, string, bool) {
	if r.Method != http.MethodPost {
		writeAgentError(w, http.StatusMethodNotAllowed, "INVALID_REQUEST", "仅支持 POST", false, requestIDFor(r), nil)
		return agentproxy.ChatRequest{}, "", false
	}

	var request agentproxy.ChatRequest
	if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&request); err != nil {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "请求格式错误", false, requestIDFor(r), nil)
		return agentproxy.ChatRequest{}, "", false
	}
	requestID := requestIDFor(r)
	request.RequestID = requestID
	request.SessionID = strings.TrimSpace(request.SessionID)
	request.Message = strings.TrimSpace(request.Message)
	if request.SessionID == "" || request.Message == "" {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "session_id 和 message 不能为空", false, requestID, nil)
		return agentproxy.ChatRequest{}, "", false
	}
	if h.AgentProxy == nil {
		writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "Agent 服务暂不可用", true, requestID, nil)
		return agentproxy.ChatRequest{}, "", false
	}
	return request, requestID, true
}

// POST /api/agent/chat
func (h *Handler) handleAgentChat(w http.ResponseWriter, r *http.Request) {
	request, requestID, ok := h.decodeAgentChatRequest(w, r)
	if !ok {
		return
	}

	response, err := h.AgentProxy.Chat(r.Context(), request)
	if err != nil {
		log.Printf("agent proxy request failed: %v", err)
		writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "Agent 服务暂不可用", true, requestID, nil)
		return
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		status, code, retryable := agentUpstreamError(response.StatusCode)
		writeAgentError(w, status, code, "Agent 服务返回错误", retryable, requestID, map[string]any{
			"upstream_status": response.StatusCode,
		})
		return
	}
	if !json.Valid(response.Body) {
		writeAgentError(w, http.StatusBadGateway, "UPSTREAM_ERROR", "Agent 服务返回格式错误", true, requestID, nil)
		return
	}

	contentType := response.ContentType
	if contentType == "" {
		contentType = "application/json"
	}
	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(response.StatusCode)
	w.Write(response.Body)
}

// POST /api/agent/chat/stream
//
// Relays the Agent's Server-Sent Events to the caller unchanged.  Go does not
// parse the frames: the event schema is the Python service's contract, and a
// second reader of it here would be one more thing to keep in step.
func (h *Handler) handleAgentChatStream(w http.ResponseWriter, r *http.Request) {
	request, requestID, ok := h.decodeAgentChatRequest(w, r)
	if !ok {
		return
	}

	// The request context is the upstream context, so a client that goes away
	// cancels the Agent turn at its source instead of leaving it running.
	upstream, err := h.AgentProxy.ChatStream(r.Context(), request)
	if err != nil {
		var streamErr *agentproxy.StreamError
		if errors.As(err, &streamErr) {
			status, code, retryable := agentUpstreamError(streamErr.StatusCode)
			writeAgentError(w, status, code, "Agent 服务返回错误", retryable, requestID, map[string]any{
				"upstream_status": streamErr.StatusCode,
			})
			return
		}
		log.Printf("agent stream request failed: %v", err)
		writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "Agent 服务暂不可用", true, requestID, nil)
		return
	}
	defer upstream.Close()

	// A server that cannot flush cannot stream; say so before committing 200,
	// because once the headers are out the only way to report a failure left is
	// an error event inside a body the caller may already have given up on.
	flusher, ok := w.(http.Flusher)
	if !ok {
		writeAgentError(w, http.StatusInternalServerError, "INTERNAL_ERROR", "服务端不支持流式响应", false, requestID, nil)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream; charset=utf-8")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)
	flusher.Flush()

	relayStream(w, flusher, upstream)
}

// GET /api/agent/session/{session_id}
//
// Reads back what one session has already produced, so a refreshed page can
// restore the right-hand panel without replaying the event stream.  Go does
// not interpret the payload: the fields belong to the Agent service contract.
func (h *Handler) handleAgentSession(w http.ResponseWriter, r *http.Request) {
	requestID := requestIDFor(r)
	if r.Method != http.MethodGet {
		writeAgentError(w, http.StatusMethodNotAllowed, "INVALID_REQUEST", "仅支持 GET", false, requestID, nil)
		return
	}
	sessionID := strings.TrimSpace(strings.TrimPrefix(r.URL.Path, "/api/agent/session/"))
	if sessionID == "" {
		writeAgentError(w, http.StatusBadRequest, "INVALID_REQUEST", "session_id 不能为空", false, requestID, nil)
		return
	}
	if h.AgentProxy == nil {
		writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "Agent 服务暂不可用", true, requestID, nil)
		return
	}

	response, err := h.AgentProxy.Session(r.Context(), sessionID, requestID)
	if err != nil {
		log.Printf("agent session request failed: %v", err)
		writeAgentError(w, http.StatusServiceUnavailable, "CAPABILITY_UNAVAILABLE", "Agent 服务暂不可用", true, requestID, nil)
		return
	}
	if response.StatusCode < 200 || response.StatusCode >= 300 {
		status, code, retryable := agentUpstreamError(response.StatusCode)
		writeAgentError(w, status, code, "该会话暂无可用状态", retryable, requestID, map[string]any{
			"upstream_status": response.StatusCode,
		})
		return
	}
	if !json.Valid(response.Body) {
		writeAgentError(w, http.StatusBadGateway, "UPSTREAM_ERROR", "Agent 服务返回格式错误", true, requestID, nil)
		return
	}

	contentType := response.ContentType
	if contentType == "" {
		contentType = "application/json"
	}
	w.Header().Set("Content-Type", contentType)
	w.WriteHeader(response.StatusCode)
	w.Write(response.Body)
}

type agentConfigModel struct {
	Provider   string `json:"provider"`
	Model      string `json:"model"`
	APIBaseURL string `json:"api_base_url"`
	APIKey     string `json:"api_key"`
}

type agentConfigPayload struct {
	Supervisor agentConfigModel `json:"supervisor"`
	Script     agentConfigModel `json:"script"`
}

// GET / PUT /api/agent/config
//
// The Agent panel fills the model settings the Python service needs.  Secrets
// are written with the same machine-bound encryption the rest of the settings
// use, and the GET response only ever carries the mask.
func (h *Handler) handleAgentConfig(w http.ResponseWriter, r *http.Request) {
	if h.Store == nil || h.Store.Config == "" {
		writeError(w, http.StatusServiceUnavailable, "设置文件不可用")
		return
	}

	switch r.Method {
	case http.MethodGet:
		cfg, err := store.LoadAgentConfig(h.Store.Config)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "读取 Agent 设置失败")
			return
		}
		writeJSON(w, maskAgentConfig(cfg))

	case http.MethodPut:
		var payload agentConfigPayload
		if err := json.NewDecoder(io.LimitReader(r.Body, 1<<20)).Decode(&payload); err != nil {
			writeError(w, http.StatusBadRequest, "请求格式错误")
			return
		}
		existing, err := store.LoadAgentConfig(h.Store.Config)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "读取 Agent 设置失败")
			return
		}
		next := store.AgentConfig{
			Supervisor: mergeAgentModel(payload.Supervisor, existing.Supervisor),
			Script:     mergeAgentModel(payload.Script, existing.Script),
		}
		if strings.TrimSpace(next.Supervisor.Model) == "" ||
			strings.TrimSpace(next.Supervisor.APIKey) == "" {
			writeError(w, http.StatusBadRequest, "Supervisor 的模型名与 API Key 都不能为空")
			return
		}
		if err := store.SaveAgentConfig(h.Store.Config, next); err != nil {
			writeError(w, http.StatusInternalServerError, "保存 Agent 设置失败")
			return
		}
		writeJSON(w, map[string]string{"status": "ok"})

	default:
		writeError(w, http.StatusMethodNotAllowed, "仅支持 GET / PUT")
	}
}

// maskAgentConfig replaces both keys with the shared mask for transport.
func maskAgentConfig(cfg store.AgentConfig) agentConfigPayload {
	model := func(source store.AgentModelConfig) agentConfigModel {
		masked := source.APIKey
		if strings.TrimSpace(masked) != "" {
			masked = store.MaskedValue
		}
		return agentConfigModel{
			Provider:   source.Provider,
			Model:      source.Model,
			APIBaseURL: source.APIBaseURL,
			APIKey:     masked,
		}
	}
	return agentConfigPayload{Supervisor: model(cfg.Supervisor), Script: model(cfg.Script)}
}

// mergeAgentModel keeps the stored secret when the client sent back the mask.
func mergeAgentModel(incoming agentConfigModel, existing store.AgentModelConfig) store.AgentModelConfig {
	apiKey := incoming.APIKey
	if apiKey == store.MaskedValue {
		apiKey = existing.APIKey
	}
	provider := strings.TrimSpace(incoming.Provider)
	if provider == "" {
		provider = existing.Provider
	}
	baseURL := strings.TrimSpace(incoming.APIBaseURL)
	if baseURL == "" {
		baseURL = existing.APIBaseURL
	}
	return store.AgentModelConfig{
		Provider:   provider,
		Model:      strings.TrimSpace(incoming.Model),
		APIBaseURL: baseURL,
		APIKey:     strings.TrimSpace(apiKey),
	}
}

// relayStream copies SSE bytes through, flushing after every read.//
// Each flush is what makes an event visible now rather than whenever a buffer
// happens to fill.  Returning on a read or write error covers both ends of the
// disconnect: a dead client fails the write, and a cancelled request context
// fails the read.
func relayStream(w http.ResponseWriter, flusher http.Flusher, upstream io.Reader) {
	buffer := make([]byte, 4096)
	for {
		read, readErr := upstream.Read(buffer)
		if read > 0 {
			if _, writeErr := w.Write(buffer[:read]); writeErr != nil {
				return
			}
			flusher.Flush()
		}
		if readErr != nil {
			return
		}
	}
}

func requestIDFor(r *http.Request) string {
	requestID := strings.TrimSpace(r.Header.Get("X-Request-ID"))
	if requestID != "" {
		return requestID
	}
	return "req_" + randomRequestID()
}

func randomRequestID() string {
	var raw [16]byte
	if _, err := io.ReadFull(rand.Reader, raw[:]); err == nil {
		return hex.EncodeToString(raw[:])
	}
	return strconv.FormatInt(time.Now().UnixNano(), 10)
}

func agentUpstreamError(upstreamStatus int) (status int, code string, retryable bool) {
	switch upstreamStatus {
	case http.StatusBadRequest:
		return http.StatusBadRequest, "INVALID_REQUEST", false
	case http.StatusUnauthorized, http.StatusForbidden:
		return upstreamStatus, "UNAUTHORIZED", false
	case http.StatusNotFound:
		return http.StatusNotFound, "CAPABILITY_NOT_FOUND", false
	case http.StatusConflict:
		return http.StatusConflict, "CAPABILITY_CONFLICT", false
	case http.StatusUnprocessableEntity:
		return http.StatusUnprocessableEntity, "CAPABILITY_REJECTED", false
	case http.StatusGatewayTimeout:
		return http.StatusGatewayTimeout, "CAPABILITY_TIMEOUT", true
	case http.StatusServiceUnavailable, http.StatusTooManyRequests:
		return upstreamStatus, "CAPABILITY_UNAVAILABLE", true
	default:
		return http.StatusBadGateway, "UPSTREAM_ERROR", upstreamStatus >= 500
	}
}

func writeJSON(w http.ResponseWriter, v any) {
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, code int, msg string) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	writeJSON(w, map[string]string{"error": msg})
}

// GET /api/avatars
func (h *Handler) handleAvatars(w http.ResponseWriter, r *http.Request) {
	avatars, err := store.LoadAvatars(h.Store.Avatars)
	if err != nil {
		if !os.IsNotExist(err) {
			writeError(w, 500, "读取形象列表失败")
			return
		}
		avatars = []store.Avatar{}
	}

	// 每次读取时从蝉镜同步主站上传的定制数字人。同步失败时保留本地
	// 缓存，避免临时网络或认证问题导致主界面无法选择已有形象。
	if lister, ok := h.Chanjing.(chanjing.CustomisedPersonLister); ok {
		token, tokenErr := h.Chanjing.GetToken()
		if tokenErr == nil {
			remote, listErr := lister.ListCustomisedPersons(token, 1)
			if listErr != nil && strings.Contains(listErr.Error(), "10400") {
				h.Chanjing.InvalidateTokenCache()
				if token, tokenErr = h.Chanjing.GetToken(); tokenErr == nil {
					remote, listErr = lister.ListCustomisedPersons(token, 1)
				}
			}
			if listErr == nil {
				merged := mergeRemoteAvatars(avatars, remote)
				if err := store.SaveAvatars(h.Store.Avatars, merged); err == nil {
					avatars = merged
				}
			}
		}
	}
	writeJSON(w, avatars)
}

func mergeRemoteAvatars(local []store.Avatar, remote []chanjing.CustomisedPerson) []store.Avatar {
	merged := make([]store.Avatar, 0, len(local)+len(remote))
	indices := make(map[string]int, len(local)+len(remote))
	for _, avatar := range local {
		if strings.TrimSpace(avatar.ID) == "" {
			continue
		}
		indices[avatar.ID] = len(merged)
		merged = append(merged, avatar)
	}
	for _, person := range remote {
		if strings.TrimSpace(person.ID) == "" || person.Status != 2 || person.IsOpen == 0 {
			continue
		}
		preview := strings.TrimSpace(person.PicURL)
		if preview == "" {
			preview = strings.TrimSpace(person.PreviewURL)
		}
		avatar := store.Avatar{ID: person.ID, Name: person.Name, PreviewURL: preview}
		if strings.TrimSpace(avatar.Name) == "" {
			avatar.Name = avatar.ID
		}
		if index, exists := indices[avatar.ID]; exists {
			merged[index] = avatar
		} else {
			indices[avatar.ID] = len(merged)
			merged = append(merged, avatar)
		}
	}
	return merged
}

// GET /api/voices
func (h *Handler) handleVoices(w http.ResponseWriter, r *http.Request) {
	voices, err := store.LoadVoices(h.Store.Voices)
	if err != nil {
		writeError(w, 500, "读取人声列表失败")
		return
	}
	writeJSON(w, voices)
}

// POST /api/scripts/generate
func (h *Handler) handleGenerateScript(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		writeError(w, 405, "仅支持 POST")
		return
	}

	var req struct {
		Topic string `json:"topic"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, 400, "请求格式错误")
		return
	}
	if strings.TrimSpace(req.Topic) == "" {
		writeError(w, 400, "话题不能为空")
		return
	}

	cfg, err := store.LoadConfig(h.Store.Config)
	if err != nil {
		writeError(w, 500, "读取配置失败")
		return
	}

	if cfg.LLM.APIURL == "" || cfg.LLM.APIKey == "" {
		writeError(w, 400, "大模型未配置，请在设置中配置 API 地址和密钥")
		return
	}

	prompt, files, err := skill.BuildPrompt(&cfg, req.Topic)
	if err != nil {
		writeError(w, 500, "Skill 源读取失败: "+err.Error())
		return
	}
	if len(files) > 0 {
		log.Printf("skill files loaded: %s", strings.Join(files, ", "))
	}

	// GitHub 源同步成功后持久化缓存时间
	if cfg.Skill.Source == "github" {
		store.SaveConfig(h.Store.Config, cfg)
	}

	script, err := llm.GenerateScript(cfg.LLM.APIURL, cfg.LLM.APIKey, cfg.LLM.Model, req.Topic, prompt)
	if err != nil {
		writeError(w, 500, "生成失败: "+err.Error())
		return
	}

	writeJSON(w, map[string]string{"script": script})
}

// POST /api/topic-search
func (h *Handler) handleTopicSearch(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		writeError(w, http.StatusMethodNotAllowed, "仅支持 POST")
		return
	}
	var req struct {
		Query string `json:"query"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, 16<<10)).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "请求格式错误")
		return
	}
	query := strings.TrimSpace(req.Query)
	if query == "" {
		writeError(w, http.StatusBadRequest, "搜索词不能为空")
		return
	}
	cfg, err := store.LoadConfig(h.Store.Config)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "读取配置失败")
		return
	}
	if strings.TrimSpace(cfg.LLM.APIURL) == "" || strings.TrimSpace(cfg.LLM.APIKey) == "" {
		writeError(w, http.StatusBadRequest, "大模型未配置，请在设置中配置 API 地址和密钥")
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 300*time.Second)
	defer cancel()
	var result topicsearch.Response
	if h.TopicSearch != nil {
		result, err = h.TopicSearch(ctx, query, cfg.LLM.APIURL, cfg.LLM.APIKey, cfg.LLM.Model)
	} else if h.TopicSearchWithConfig != nil {
		result, err = h.TopicSearchWithConfig(ctx, query, cfg)
	} else {
		result, err = topicsearch.NewService().SearchWithConfigFromStore(ctx, query, cfg)
	}
	if err != nil {
		log.Printf("topic search failed for %q: %v", query, err)
		writeError(w, http.StatusBadGateway, topicSearchErrorMessage(err))
		return
	}
	if result.Results == nil {
		result.Results = []topicsearch.Result{}
	}
	writeJSON(w, result)
}

func topicSearchErrorMessage(err error) string {
	message := err.Error()
	switch {
	case strings.Contains(message, "联网搜索工具未配置") || strings.Contains(message, "未配置 API Key"):
		return "搜索服务未配置，请使用本地联网搜索，或选择 Tavily/Brave 并填写对应 API Key"
	case strings.Contains(message, "AI 整理失败"):
		return "已调用 AI，但搜索结果整理失败，请检查大模型配置后重试"
	case strings.Contains(message, "web_search 未执行"):
		return "DeepSeek 内置搜索没有返回可验证的搜索调用，请重试或检查模型配置"
	case strings.Contains(message, "Responses 请求失败") || strings.Contains(message, "context deadline exceeded"):
		return "DeepSeek 搜索请求超时或失败，请稍后重试"
	case strings.Contains(message, "搜索服务没有返回"):
		return "本地联网搜索没有返回可用结果，请更换关键词后重试"
	default:
		return "搜索失败，请稍后重试"
	}
}

// GET /api/topics, POST /api/topics, DELETE /api/topics?source_url=...
func (h *Handler) handleTopics(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case http.MethodGet:
		topics, err := store.LoadTopics(h.Store.Topics)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "读取选题库失败")
			return
		}
		writeJSON(w, map[string]any{"topics": topics})
	case http.MethodPost:
		var req struct {
			Title     string             `json:"title"`
			Type      string             `json:"type"`
			SourceURL string             `json:"source_url"`
			RawInfo   topicsearch.Result `json:"raw_info"`
		}
		if err := json.NewDecoder(io.LimitReader(r.Body, 128<<10)).Decode(&req); err != nil {
			writeError(w, http.StatusBadRequest, "请求格式错误")
			return
		}
		parsedURL, urlErr := url.Parse(strings.TrimSpace(req.SourceURL))
		validURL := urlErr == nil && (parsedURL.Scheme == "http" || parsedURL.Scheme == "https") && parsedURL.Host != ""
		if strings.TrimSpace(req.Title) == "" || !validURL {
			writeError(w, http.StatusBadRequest, "选题标题不能为空，原文链接必须是有效的 HTTP(S) 地址")
			return
		}
		topic := store.SavedTopic{
			Title: req.Title, Type: req.Type, SourceURL: req.SourceURL,
			RawInfo: req.RawInfo, CreatedAt: time.Now().UTC().Format(time.RFC3339), Status: "saved",
		}
		saved, added, err := store.AddTopic(h.Store.Topics, topic)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "保存选题失败")
			return
		}
		writeJSON(w, map[string]any{"saved": added, "topic": saved})
	case http.MethodDelete:
		sourceURL := strings.TrimSpace(r.URL.Query().Get("source_url"))
		parsedURL, urlErr := url.Parse(sourceURL)
		validURL := urlErr == nil && (parsedURL.Scheme == "http" || parsedURL.Scheme == "https") && parsedURL.Host != ""
		if !validURL {
			writeError(w, http.StatusBadRequest, "source_url 必须是有效的 HTTP(S) 地址")
			return
		}
		deleted, err := store.DeleteTopic(h.Store.Topics, sourceURL)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "删除选题失败")
			return
		}
		if !deleted {
			writeError(w, http.StatusNotFound, "选题不存在")
			return
		}
		writeJSON(w, map[string]any{"deleted": true, "source_url": sourceURL})
	default:
		writeError(w, http.StatusMethodNotAllowed, "仅支持 GET / POST / DELETE")
	}
}

const taskRetention = 30 * 24 * time.Hour

// POST /api/tasks, GET /api/tasks, DELETE /api/tasks
func (h *Handler) handleTasks(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case "POST":
		h.createTask(w, r)
	case "GET":
		_, _ = store.PruneExpiredTasks(h.Store.Tasks, time.Now().UTC().Add(-taskRetention))
		tasks, err := store.LoadTasks(h.Store.Tasks)
		if err != nil {
			writeJSON(w, map[string]any{"tasks": []store.Task{}})
			return
		}
		writeJSON(w, map[string]any{"tasks": tasks})
	case "DELETE":
		removed, err := store.ClearTerminalTasks(h.Store.Tasks)
		if err != nil {
			writeError(w, 500, "清空任务记录失败")
			return
		}
		writeJSON(w, map[string]any{"removed": removed})
	default:
		writeError(w, 405, "仅支持 POST / GET / DELETE")
	}
}

// GET /api/tasks/:id, POST /api/tasks/:id/retry, POST /api/tasks/:id/refresh
func (h *Handler) handleTaskByID(w http.ResponseWriter, r *http.Request) {
	path := strings.TrimPrefix(r.URL.Path, "/api/tasks/")
	if path == "" {
		writeError(w, 400, "缺少 task_id")
		return
	}

	// POST /api/tasks/:id/retry
	if strings.HasSuffix(path, "/retry") && r.Method == "POST" {
		taskID := strings.TrimSuffix(path, "/retry")
		h.retryTask(w, r, taskID)
		return
	}
	if strings.HasSuffix(path, "/refresh") && r.Method == "POST" {
		taskID := strings.TrimSuffix(path, "/refresh")
		h.refreshTask(w, r, taskID)
		return
	}
	if r.Method == "DELETE" {
		err := store.DeleteTask(h.Store.Tasks, path)
		switch {
		case errors.Is(err, store.ErrTaskNotFound):
			writeError(w, 404, err.Error())
		case errors.Is(err, store.ErrTaskInProgress):
			writeError(w, 409, err.Error())
		case err != nil:
			writeError(w, 500, "删除任务记录失败")
		default:
			writeJSON(w, map[string]bool{"deleted": true})
		}
		return
	}

	taskID := path
	tasks, err := store.LoadTasks(h.Store.Tasks)
	if err != nil {
		writeError(w, 500, "读取任务列表失败")
		return
	}
	for _, t := range tasks {
		if t.TaskID == taskID {
			writeJSON(w, t)
			return
		}
	}
	writeError(w, 404, "任务不存在")
}

func (h *Handler) refreshTask(w http.ResponseWriter, r *http.Request, taskID string) {
	if h.Scheduler == nil {
		writeError(w, 503, "浠诲姟鍒锋柊鍔熻兘涓嶅彲鐢?")
		return
	}
	task, err := h.Scheduler.Refresh(taskID)
	if err != nil {
		writeError(w, 503, "鍒锋柊澶辫触: "+err.Error())
		return
	}
	writeJSON(w, map[string]any{
		"status": "ok",
		"task":   task,
	})
}

func (h *Handler) retryTask(w http.ResponseWriter, r *http.Request, taskID string) {
	tasks, err := store.LoadTasks(h.Store.Tasks)
	if err != nil {
		writeError(w, 500, "读取任务列表失败")
		return
	}

	var oldTask *store.Task
	for i := range tasks {
		if tasks[i].TaskID == taskID {
			oldTask = &tasks[i]
			break
		}
	}
	if oldTask == nil {
		writeError(w, 404, "任务不存在")
		return
	}

	// Params 经 JSON 存取后变成 map[string]any，序列化回来再反序列化回结构体
	paramsBytes, err := json.Marshal(oldTask.Params)
	if err != nil {
		writeError(w, 500, "解析任务参数失败")
		return
	}
	var cjReq chanjing.CreateVideoRequest
	if err := json.Unmarshal(paramsBytes, &cjReq); err != nil {
		writeError(w, 500, "还原任务参数失败: "+err.Error())
		return
	}
	if cjReq.AudioSource == 0 {
		audioSource, err := h.voiceAudioSource(cjReq.Audio.TTS.AudioMan)
		if err != nil {
			writeError(w, 500, "读取人声配置失败")
			return
		}
		cjReq.AudioSource = audioSource
	}

	token, err := h.Chanjing.GetToken()
	if err != nil {
		writeError(w, 503, "蝉镜认证失败: "+err.Error())
		return
	}

	newTaskID, err := h.Chanjing.CreateVideo(token, cjReq)
	if err != nil {
		writeError(w, 503, "重试失败: "+err.Error())
		return
	}

	newTask := store.Task{
		TaskID:    newTaskID,
		Status:    "pending",
		Script:    oldTask.Script,
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
		Params:    cjReq,
	}
	if newTask.Script == "" && len(cjReq.Audio.TTS.Text) > 0 {
		newTask.Script = cjReq.Audio.TTS.Text[0]
	}

	tasks = append(tasks, newTask)
	if err := store.SaveTasks(h.Store.Tasks, tasks); err != nil {
		writeError(w, 500, "保存任务列表失败")
		return
	}

	h.Scheduler.Add(newTaskID, token)

	writeJSON(w, map[string]string{
		"task_id": newTaskID,
		"status":  "pending",
	})
}

func (h *Handler) createTask(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Script              string  `json:"script"`
		AvatarID            string  `json:"avatar_id"`
		AudioManID          string  `json:"audio_man_id"`
		Speed               float64 `json:"speed"`
		Volume              int     `json:"volume"`
		BgColor             string  `json:"bg_color"`
		Resolution          string  `json:"resolution"`
		SubtitleEnabled     bool    `json:"subtitle_enabled"`
		FigureType          string  `json:"figure_type"`
		DriveMode           string  `json:"drive_mode"`
		Backway             int     `json:"backway"`
		Language            string  `json:"language"`
		ComplianceWatermark bool    `json:"compliance_watermark"`
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		writeError(w, 400, "读取请求体失败")
		return
	}
	if err := json.Unmarshal(body, &req); err != nil {
		writeError(w, 400, "请求格式错误: "+err.Error())
		return
	}

	if req.Script == "" {
		writeError(w, 400, "稿件不能为空")
		return
	}

	// defaults — 对齐 n8n 工作流
	if req.Speed == 0 {
		req.Speed = 1.0
	}
	if req.Volume == 0 {
		req.Volume = 100
	}
	if req.FigureType == "" {
		req.FigureType = "stand_body"
	}
	if req.DriveMode == "" {
		req.DriveMode = "random"
	}
	if req.Backway == 0 {
		req.Backway = 1
	}
	if req.Language == "" {
		req.Language = "cn"
	}
	if req.BgColor == "" {
		req.BgColor = "#ffffff"
	}

	audioSource, err := h.voiceAudioSource(req.AudioManID)
	if err != nil {
		writeError(w, 500, "读取人声配置失败")
		return
	}

	scw, sch := 1080, 1920
	if req.Resolution == "720P" {
		scw, sch = 720, 1280
	}

	cjReq := chanjing.CreateVideoRequest{
		Person: chanjing.PersonConfig{
			ID:         req.AvatarID,
			X:          0,
			Y:          0,
			Width:      scw,
			Height:     sch,
			FigureType: req.FigureType,
			DriveMode:  req.DriveMode,
			Backway:    req.Backway,
		},
		Audio: chanjing.AudioConfig{
			TTS: chanjing.TTSConfig{
				Text:     []string{req.Script},
				Speed:    req.Speed,
				AudioMan: req.AudioManID,
			},
			WavURL:   "",
			Type:     "tts",
			Volume:   req.Volume,
			Language: req.Language,
		},
		BgColor:                req.BgColor,
		ScreenWidth:            scw,
		ScreenHeight:           sch,
		SubtitleConfig:         chanjing.SubtitleConfig{Show: req.SubtitleEnabled},
		AddComplianceWatermark: req.ComplianceWatermark,
		Source:                 1,
		AudioSource:            audioSource,
	}

	token, err := h.Chanjing.GetToken()
	if err != nil {
		writeError(w, 503, "蝉镜认证失败: "+err.Error())
		return
	}

	taskID, err := h.Chanjing.CreateVideo(token, cjReq)
	if err != nil {
		writeError(w, 503, "创建视频任务失败: "+err.Error())
		return
	}

	newTask := store.Task{
		TaskID:    taskID,
		Status:    "pending",
		Script:    req.Script,
		CreatedAt: time.Now().UTC().Format(time.RFC3339),
		Params:    cjReq,
	}

	tasks, err := store.LoadTasks(h.Store.Tasks)
	if err != nil {
		writeError(w, 500, "读取任务列表失败")
		return
	}
	tasks = append(tasks, newTask)
	if err := store.SaveTasks(h.Store.Tasks, tasks); err != nil {
		writeError(w, 500, "保存任务列表失败")
		return
	}

	h.Scheduler.Add(taskID, token)

	writeJSON(w, map[string]string{
		"task_id": taskID,
		"status":  "pending",
	})
}

func (h *Handler) voiceAudioSource(voiceID string) (int, error) {
	voices, err := store.LoadVoices(h.Store.Voices)
	if err != nil {
		if os.IsNotExist(err) {
			return 0, nil
		}
		return 0, err
	}
	for _, voice := range voices {
		if voice.ID == voiceID {
			return voice.AudioSource, nil
		}
	}
	return 0, nil
}

// GET /api/settings, PUT /api/settings
func (h *Handler) handleSettings(w http.ResponseWriter, r *http.Request) {
	switch r.Method {
	case "GET":
		cfg, err := store.LoadConfig(h.Store.Config)
		if err != nil {
			// A stored secret that cannot be decrypted on this machine (a moved
			// settings file, a renamed host) must not read as "no key at all":
			// the page would then save the blank form back and erase it. Answer
			// with the stored values masked, plus the reason.
			raw, rawErr := store.LoadConfigFile(h.Store.Config)
			if rawErr != nil {
				writeJSON(w, store.Config{})
				return
			}
			masked := raw
			if masked.Chanjing.SecretKey != "" {
				masked.Chanjing.SecretKey = store.MaskedValue
			}
			if masked.LLM.APIKey != "" {
				masked.LLM.APIKey = store.MaskedValue
			}
			if masked.Search.APIKey != "" {
				masked.Search.APIKey = store.MaskedValue
			}
			if masked.Agent.Supervisor.APIKey != "" {
				masked.Agent.Supervisor.APIKey = store.MaskedValue
			}
			if masked.Agent.Script.APIKey != "" {
				masked.Agent.Script.APIKey = store.MaskedValue
			}
			writeJSON(w, map[string]any{
				"chanjing":         masked.Chanjing,
				"llm":              masked.LLM,
				"search":           masked.Search,
				"skill":            masked.Skill,
				"agent":            masked.Agent,
				"secrets_readable": false,
				"notice": "已保存的密钥在这台机器上解不开（配置可能来自另一台机器，或主机名变了）。" +
					"其余设置照常可用；请重新填写密钥后保存，未重填的密文会原样保留。",
			})
			return
		}
		if cfg.Chanjing.SecretKey != "" {
			cfg.Chanjing.SecretKey = store.MaskedValue
		}
		if cfg.LLM.APIKey != "" {
			cfg.LLM.APIKey = store.MaskedValue
		}
		if cfg.Search.APIKey != "" {
			cfg.Search.APIKey = store.MaskedValue
		}
		// The Agent section shares this file, so it is masked here as well: a
		// settings read must never carry a model key in clear text.
		if cfg.Agent.Supervisor.APIKey != "" {
			cfg.Agent.Supervisor.APIKey = store.MaskedValue
		}
		if cfg.Agent.Script.APIKey != "" {
			cfg.Agent.Script.APIKey = store.MaskedValue
		}
		writeJSON(w, cfg)

	case "PUT":
		var cfg store.Config
		if err := json.NewDecoder(r.Body).Decode(&cfg); err != nil {
			writeError(w, 400, "请求格式错误")
			return
		}
		// One read of the stored settings backs every merge below: a field the
		// panel left blank (or sent back as the mask) keeps what is already
		// saved instead of erasing it.
		old, loadErr := store.LoadConfig(h.Store.Config)
		if loadErr != nil {
			// The stored ciphertext stays the merge source when it cannot be read
			// here: erasing a key the user typed on another machine would be
			// worse than carrying it forward untouched (encryptOrKeep leaves an
			// already-sealed value exactly as it is).
			old, _ = store.LoadConfigFile(h.Store.Config)
		}
		if strings.TrimSpace(cfg.Chanjing.AppID) == "" {
			cfg.Chanjing.AppID = old.Chanjing.AppID
		}
		if cfg.Chanjing.SecretKey == store.MaskedValue || strings.TrimSpace(cfg.Chanjing.SecretKey) == "" {
			cfg.Chanjing.SecretKey = old.Chanjing.SecretKey
		}
		if cfg.LLM.APIKey == store.MaskedValue || strings.TrimSpace(cfg.LLM.APIKey) == "" {
			cfg.LLM.APIKey = old.LLM.APIKey
		}
		if cfg.Search.APIKey == store.MaskedValue || strings.TrimSpace(cfg.Search.APIKey) == "" {
			cfg.Search.APIKey = old.Search.APIKey
		}
		// The Agent model settings are edited in the Agent panel, not here, so
		// an empty section means "unchanged" rather than "clear it". Without
		// this, saving this page wiped the Agent's own configuration.
		if strings.TrimSpace(cfg.Agent.Supervisor.Model) == "" &&
			strings.TrimSpace(cfg.Agent.Supervisor.APIKey) == "" {
			cfg.Agent = old.Agent
		}
		// 蝉镜 credentials are only needed to submit a digital human. Requiring
		// them here made the whole page unsaveable for anyone who only wants to
		// configure the model and the search provider.
		if err := store.SaveConfig(h.Store.Config, cfg); err != nil {
			writeError(w, 500, "保存设置失败")
			return
		}
		h.Chanjing.Configure(cfg.Chanjing.AppID, cfg.Chanjing.SecretKey)
		writeJSON(w, map[string]string{"status": "ok"})

	default:
		writeError(w, 405, "仅支持 GET / PUT")
	}
}
