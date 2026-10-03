package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"strings"
	"time"
)

type message struct {
	Role    string `json:"role"`
	Content string `json:"content"`
}

// ToolDefinition and ToolMessage are the small OpenAI-compatible subset used
// by the topic-search Agent. Keeping these types here lets the application
// execute a local tool while DeepSeek remains responsible for deciding when it
// needs that tool.
type ToolDefinition struct {
	Type     string             `json:"type"`
	Function ToolFunctionSchema `json:"function"`
}

type ToolFunctionSchema struct {
	Name        string `json:"name"`
	Description string `json:"description"`
	Parameters  any    `json:"parameters"`
	Strict      bool   `json:"strict,omitempty"`
}

type ToolCall struct {
	ID       string           `json:"id"`
	Type     string           `json:"type"`
	Function ToolCallFunction `json:"function"`
}

type ToolCallFunction struct {
	Name      string `json:"name"`
	Arguments string `json:"arguments"`
}

type ToolMessage struct {
	Role             string     `json:"role"`
	Content          string     `json:"content,omitempty"`
	ToolCalls        []ToolCall `json:"tool_calls,omitempty"`
	ToolCallID       string     `json:"tool_call_id,omitempty"`
	ReasoningContent string     `json:"reasoning_content,omitempty"`
}

type ToolChatResult struct {
	Content          string
	ToolCalls        []ToolCall
	ReasoningContent string
	FinishReason     string
}

// ResponsesJSONResult is the final machine-readable message returned by a
// Responses API request. The web_search call items are handled by DeepSeek's
// built-in tool and are intentionally not exposed as chat-completion tool
// calls to the caller.
type ResponsesJSONResult struct {
	Content            string
	WebSearchCallCount int
	// WebSearchURLs contains URLs from open_page actions returned by the
	// built-in web_search tool. The topic-search layer uses these as the
	// evidence boundary for model-produced source_url fields.
	WebSearchURLs []string
}

// A built-in web_search call can spend well over a minute gathering and
// opening several pages. Keep the two transport budgets explicit: the
// Responses endpoint gets the larger budget, while ordinary generation also
// needs more than the old 90 seconds when the model is under load.
const standardRequestTimeout = 180 * time.Second
const responsesRequestTimeout = 240 * time.Second

type chatRequest struct {
	Model    string    `json:"model"`
	Messages []message `json:"messages"`
}

type toolChatRequest struct {
	Model      string           `json:"model"`
	Messages   []ToolMessage    `json:"messages"`
	Tools      []ToolDefinition `json:"tools,omitempty"`
	ToolChoice string           `json:"tool_choice,omitempty"`
	Thinking   thinkingConfig   `json:"thinking"`
}

type thinkingConfig struct {
	Type string `json:"type"`
}

type chatResponse struct {
	Choices []struct {
		Message message `json:"message"`
	} `json:"choices"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

type toolChatResponse struct {
	Choices []struct {
		FinishReason string `json:"finish_reason"`
		Message      struct {
			Role             string     `json:"role"`
			Content          string     `json:"content"`
			ReasoningContent string     `json:"reasoning_content"`
			ToolCalls        []ToolCall `json:"tool_calls"`
		} `json:"message"`
	} `json:"choices"`
	Error *struct {
		Message string `json:"message"`
	} `json:"error,omitempty"`
}

// ScriptGenerator 定义口播稿生成接口，便于测试时 mock
type ScriptGenerator interface {
	GenerateScript(apiURL, apiKey, model, topic, systemPrompt string) (string, error)
}

// GenerateScript 调用 OpenAI 兼容 API 生成口播稿
func GenerateScript(apiURL, apiKey, model, topic, systemPrompt string) (string, error) {
	req := chatRequest{
		Model: model,
		Messages: []message{
			{Role: "system", Content: systemPrompt},
			{Role: "user", Content: fmt.Sprintf("请根据以下话题生成口播稿：\n\n%s", topic)},
		},
	}

	content, err := chat(apiURL, apiKey, model, req.Messages)
	if err != nil {
		return "", err
	}
	return content, nil
}

// GenerateJSON 调用 OpenAI 兼容 API，并把模型返回的 JSON（也兼容 ```json 代码块）
// 解析到 out。搜索模块使用它来约束 LLM 只返回机器可校验的数据。
func GenerateJSON(apiURL, apiKey, model, systemPrompt, userPrompt string, out any) error {
	return GenerateJSONContext(context.Background(), apiURL, apiKey, model, systemPrompt, userPrompt, out)
}

// GenerateJSONContext 是 GenerateJSON 的可取消版本，供长耗时的搜索流程使用。
func GenerateJSONContext(ctx context.Context, apiURL, apiKey, model, systemPrompt, userPrompt string, out any) error {
	content, err := chatContext(ctx, apiURL, apiKey, model, []message{
		{Role: "system", Content: systemPrompt},
		{Role: "user", Content: userPrompt},
	})
	if err != nil {
		return err
	}
	content = cleanJSON(content)
	if err := json.Unmarshal([]byte(content), out); err != nil {
		return fmt.Errorf("LLM JSON 解析失败: %w", err)
	}
	return nil
}

// ResponsesJSONContext calls the DeepSeek Responses API with its built-in
// web_search tool and returns the final output_text message. Unlike the local
// Chat Completions adapter below, this path does not ask the Go process to
// scrape a public search-engine HTML page.
func ResponsesJSONContext(ctx context.Context, apiURL, apiKey, model, input string) (ResponsesJSONResult, error) {
	if strings.TrimSpace(apiURL) == "" {
		return ResponsesJSONResult{}, fmt.Errorf("LLM API 地址为空")
	}
	payload := map[string]any{
		"model":       model,
		"input":       input,
		"tools":       []map[string]string{{"type": "web_search"}},
		"tool_choice": map[string]string{"type": "web_search"},
		"text": map[string]any{
			"format": map[string]string{"type": "json_object"},
		},
	}
	body, err := json.Marshal(payload)
	if err != nil {
		return ResponsesJSONResult{}, fmt.Errorf("Responses 请求编码失败: %w", err)
	}
	endpoint := responsesEndpoint(apiURL)
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return ResponsesJSONResult{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+apiKey)

	log.Printf("responses request start endpoint=%q model=%q", endpoint, model)
	client := &http.Client{Timeout: responsesRequestTimeout}
	resp, err := client.Do(httpReq)
	if err != nil {
		log.Printf("responses request transport failed endpoint=%q error=%v", endpoint, err)
		return ResponsesJSONResult{}, fmt.Errorf("Responses 请求失败: %w", err)
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(resp.Body, 4<<20))
	if err != nil {
		return ResponsesJSONResult{}, fmt.Errorf("读取 Responses 响应失败: %w", err)
	}
	log.Printf("responses response received endpoint=%q status=%d bytes=%d", endpoint, resp.StatusCode, len(responseBody))

	var response struct {
		Output []struct {
			Type    string          `json:"type"`
			Action  json.RawMessage `json:"action"`
			Content json.RawMessage `json:"content"`
		} `json:"output"`
		Error *struct {
			Message string `json:"message"`
		} `json:"error,omitempty"`
	}
	if err := json.Unmarshal(responseBody, &response); err != nil {
		return ResponsesJSONResult{}, fmt.Errorf("Responses 响应解析失败: %w", err)
	}
	if response.Error != nil {
		return ResponsesJSONResult{}, fmt.Errorf("LLM 返回错误: %s", response.Error.Message)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return ResponsesJSONResult{}, fmt.Errorf("Responses 请求失败，状态码 %d", resp.StatusCode)
	}

	result := ResponsesJSONResult{}
	seenURLs := make(map[string]bool)
	for _, item := range response.Output {
		if item.Type == "web_search_call" {
			result.WebSearchCallCount++
			var action struct {
				Type string `json:"type"`
				URL  string `json:"url"`
			}
			if json.Unmarshal(item.Action, &action) == nil && action.Type == "open_page" {
				if value := strings.TrimSpace(action.URL); value != "" && !seenURLs[value] {
					seenURLs[value] = true
					result.WebSearchURLs = append(result.WebSearchURLs, value)
				}
			}
		}
		if item.Type != "message" {
			continue
		}
		result.Content += responsesOutputText(item.Content)
	}
	if strings.TrimSpace(result.Content) == "" {
		return ResponsesJSONResult{}, fmt.Errorf("Responses 未返回最终内容")
	}
	return result, nil
}

func responsesEndpoint(apiURL string) string {
	endpoint := strings.TrimRight(strings.TrimSpace(apiURL), "/")
	if strings.HasSuffix(endpoint, "/responses") {
		return endpoint
	}
	if strings.HasSuffix(endpoint, "/v1") {
		endpoint = strings.TrimSuffix(endpoint, "/v1")
	}
	return endpoint + "/responses"
}

func responsesOutputText(raw json.RawMessage) string {
	if len(raw) == 0 {
		return ""
	}
	var text string
	if json.Unmarshal(raw, &text) == nil {
		return text
	}
	var parts []struct {
		Type string `json:"type"`
		Text string `json:"text"`
	}
	if json.Unmarshal(raw, &parts) != nil {
		return ""
	}
	var result strings.Builder
	for _, part := range parts {
		if part.Type == "output_text" {
			result.WriteString(part.Text)
		}
	}
	return result.String()
}

// ChatWithToolsContext sends an OpenAI-compatible tool-call request and
// returns either the assistant's final content or the local tool calls it
// wants the caller to execute. The caller should append the assistant message
// and each tool result, then call this function again until ToolCalls is empty.
func ChatWithToolsContext(ctx context.Context, apiURL, apiKey, model string, messages []ToolMessage, tools []ToolDefinition, toolChoice string) (ToolChatResult, error) {
	if strings.TrimSpace(apiURL) == "" {
		return ToolChatResult{}, fmt.Errorf("LLM API 地址为空")
	}
	req := toolChatRequest{
		Model:      model,
		Messages:   messages,
		Tools:      tools,
		ToolChoice: toolChoice,
		Thinking:   thinkingConfig{Type: "disabled"},
	}
	body, err := json.Marshal(req)
	if err != nil {
		return ToolChatResult{}, fmt.Errorf("LLM 工具请求编码失败: %w", err)
	}

	endpoint := strings.TrimRight(strings.TrimSpace(apiURL), "/") + "/v1/chat/completions"
	httpReq, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint, bytes.NewReader(body))
	if err != nil {
		return ToolChatResult{}, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+apiKey)

	log.Printf("llm tool request start endpoint=%q model=%q messages=%d tools=%d choice=%q", endpoint, model, len(messages), len(tools), toolChoice)
	client := &http.Client{Timeout: standardRequestTimeout}
	resp, err := client.Do(httpReq)
	if err != nil {
		log.Printf("llm tool request transport failed endpoint=%q error=%v", endpoint, err)
		return ToolChatResult{}, fmt.Errorf("LLM 工具请求失败: %w", err)
	}
	defer resp.Body.Close()
	responseBody, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		return ToolChatResult{}, fmt.Errorf("读取 LLM 工具响应失败: %w", err)
	}
	log.Printf("llm tool response received endpoint=%q status=%d bytes=%d", endpoint, resp.StatusCode, len(responseBody))
	var cr toolChatResponse
	if err := json.Unmarshal(responseBody, &cr); err != nil {
		return ToolChatResult{}, fmt.Errorf("LLM 工具响应解析失败: %w", err)
	}
	if cr.Error != nil {
		return ToolChatResult{}, fmt.Errorf("LLM 返回错误: %s", cr.Error.Message)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return ToolChatResult{}, fmt.Errorf("LLM 工具请求失败，状态码 %d", resp.StatusCode)
	}
	if len(cr.Choices) == 0 {
		return ToolChatResult{}, fmt.Errorf("LLM 未返回工具结果")
	}
	choice := cr.Choices[0]
	return ToolChatResult{
		Content:          choice.Message.Content,
		ToolCalls:        choice.Message.ToolCalls,
		ReasoningContent: choice.Message.ReasoningContent,
		FinishReason:     choice.FinishReason,
	}, nil
}

func chat(apiURL, apiKey, model string, messages []message) (string, error) {
	return chatContext(context.Background(), apiURL, apiKey, model, messages)
}

func chatContext(ctx context.Context, apiURL, apiKey, model string, messages []message) (string, error) {
	if strings.TrimSpace(apiURL) == "" {
		return "", fmt.Errorf("LLM API 地址为空")
	}
	req := chatRequest{Model: model, Messages: messages}
	body, err := json.Marshal(req)
	if err != nil {
		return "", fmt.Errorf("LLM 请求编码失败: %w", err)
	}

	endpoint := strings.TrimRight(strings.TrimSpace(apiURL), "/") + "/v1/chat/completions"
	httpReq, err := http.NewRequestWithContext(ctx, "POST", endpoint, bytes.NewReader(body))
	if err != nil {
		return "", err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Authorization", "Bearer "+apiKey)

	log.Printf("llm request start endpoint=%q model=%q messages=%d", endpoint, model, len(req.Messages))
	client := &http.Client{Timeout: standardRequestTimeout}
	resp, err := client.Do(httpReq)
	if err != nil {
		log.Printf("llm request transport failed endpoint=%q error=%v", endpoint, err)
		return "", fmt.Errorf("LLM 请求失败: %w", err)
	}
	defer resp.Body.Close()

	responseBody, err := io.ReadAll(io.LimitReader(resp.Body, 2<<20))
	if err != nil {
		log.Printf("llm response read failed endpoint=%q status=%d error=%v", endpoint, resp.StatusCode, err)
		return "", fmt.Errorf("读取 LLM 响应失败: %w", err)
	}
	log.Printf("llm response received endpoint=%q status=%d bytes=%d", endpoint, resp.StatusCode, len(responseBody))
	var cr chatResponse
	if err := json.Unmarshal(responseBody, &cr); err != nil {
		return "", fmt.Errorf("LLM 响应解析失败: %w", err)
	}
	if cr.Error != nil {
		return "", fmt.Errorf("LLM 返回错误: %s", cr.Error.Message)
	}
	if resp.StatusCode < 200 || resp.StatusCode >= 300 {
		return "", fmt.Errorf("LLM 请求失败，状态码 %d", resp.StatusCode)
	}
	if len(cr.Choices) == 0 {
		return "", fmt.Errorf("LLM 未返回内容")
	}
	return cr.Choices[0].Message.Content, nil
}

func cleanJSON(content string) string {
	content = strings.TrimSpace(content)
	if strings.HasPrefix(content, "```") {
		if newline := strings.IndexByte(content, '\n'); newline >= 0 {
			content = content[newline+1:]
		}
		content = strings.TrimSuffix(content, "```")
	}
	content = strings.TrimSpace(content)
	// Some compatible gateways prepend a short sentence despite the JSON-only
	// instruction. Keep only the outer JSON value so the caller can still parse
	// an otherwise valid structured response.
	if start := strings.IndexAny(content, "[{"); start > 0 {
		content = content[start:]
	}
	if end := strings.LastIndexAny(content, "]}"); end >= 0 && end+1 < len(content) {
		content = content[:end+1]
	}
	return strings.TrimSpace(content)
}
