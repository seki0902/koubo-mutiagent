package topicsearch

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"sync"
	"sync/atomic"
	"time"

	"koubo-video-tool/llm"
)

// traceState is deliberately private to the topic-search pipeline. It adds
// observability through context without changing the pipeline's decisions or
// its timeout, concurrency, or provider behavior.
type traceState struct {
	rid       string
	startedAt time.Time
	deadline  time.Time
	mu        sync.Mutex
	failedAt  string
}

type traceContextKey struct{}

var traceSequence uint64

// A logger with no prefix is used so every emitted line is valid JSONL. The
// application's existing logger remains untouched for its human-readable logs.
var traceLogger = log.New(os.Stderr, "", 0)

func beginTrace(ctx context.Context, query string) (context.Context, *traceState, bool) {
	if existing, ok := ctx.Value(traceContextKey{}).(*traceState); ok && existing != nil {
		return ctx, existing, false
	}
	n := atomic.AddUint64(&traceSequence, 1)
	trace := &traceState{
		rid:       fmt.Sprintf("topic-%d-%s", time.Now().UnixNano(), fmt.Sprintf("%x", n)),
		startedAt: time.Now(),
	}
	if deadline, ok := ctx.Deadline(); ok {
		trace.deadline = deadline
	}
	ctx = context.WithValue(ctx, traceContextKey{}, trace)
	trace.emit(ctx, "request_start", map[string]any{"query": query})
	return ctx, trace, true
}

func traceFromContext(ctx context.Context) *traceState {
	trace, _ := ctx.Value(traceContextKey{}).(*traceState)
	return trace
}

func (t *traceState) remainingDeadline(ctx context.Context) int64 {
	if !t.deadline.IsZero() {
		remaining := time.Until(t.deadline).Milliseconds()
		if remaining < 0 {
			return 0
		}
		return remaining
	}
	return -1
}

func (t *traceState) emit(ctx context.Context, event string, fields map[string]any) {
	if t == nil {
		return
	}
	record := map[string]any{
		"ts":                    time.Now().UTC().Format(time.RFC3339Nano),
		"rid":                   t.rid,
		"event":                 event,
		"duration_ms":           int64(0),
		"remaining_deadline_ms": t.remainingDeadline(ctx),
	}
	for key, value := range fields {
		record[key] = value
	}
	encoded, err := json.Marshal(record)
	if err == nil {
		traceLogger.Print(string(encoded))
	}
}

func (t *traceState) markFailedAt(step string) {
	if t == nil || step == "" {
		return
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.failedAt == "" {
		t.failedAt = step
	}
}

func (t *traceState) failedStep() string {
	if t == nil {
		return ""
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.failedAt
}

func (t *traceState) endRequest(ctx context.Context) {
	deadlineExceeded := ctx.Err() == context.DeadlineExceeded
	if !deadlineExceeded && t.remainingDeadline(ctx) == 0 {
		deadlineExceeded = true
	}
	totalDuration := time.Since(t.startedAt).Milliseconds()
	t.emit(ctx, "request_end", map[string]any{
		"duration_ms":       totalDuration,
		"total_duration_ms": totalDuration,
		"deadline_exceeded": deadlineExceeded,
		"failed_at":         t.failedStep(),
	})
}

func traceGenerateJSON(ctx context.Context, trace *traceState, phase string, round int, apiURL, apiKey, model, systemPrompt, userPrompt string, out any) error {
	started := time.Now()
	trace.emit(ctx, "llm_call_start", map[string]any{
		"phase":          phase,
		"round":          round,
		"tool_calls":     0,
		"has_tool_calls": false,
	})
	err := llm.GenerateJSONContext(ctx, apiURL, apiKey, model, systemPrompt, userPrompt, out)
	trace.emit(ctx, "llm_call_end", map[string]any{
		"phase":          phase,
		"round":          round,
		"duration_ms":    startedDuration(started),
		"tool_calls":     0,
		"has_tool_calls": false,
		"error":          errorText(err),
	})
	return err
}

func traceChatWithTools(ctx context.Context, trace *traceState, phase string, round int, apiURL, apiKey, model string, messages []llm.ToolMessage, tools []llm.ToolDefinition, toolChoice string) (llm.ToolChatResult, error) {
	started := time.Now()
	trace.emit(ctx, "llm_call_start", map[string]any{
		"phase":          phase,
		"round":          round,
		"tool_choice":    toolChoice,
		"tools":          len(tools),
		"tool_calls":     0,
		"has_tool_calls": false,
	})
	result, err := llm.ChatWithToolsContext(ctx, apiURL, apiKey, model, messages, tools, toolChoice)
	if err != nil {
		trace.markFailedAt(phase)
	}
	trace.emit(ctx, "llm_call_end", map[string]any{
		"phase":          phase,
		"round":          round,
		"tool_choice":    toolChoice,
		"duration_ms":    startedDuration(started),
		"tool_calls":     len(result.ToolCalls),
		"has_tool_calls": len(result.ToolCalls) > 0,
		"error":          errorText(err),
	})
	return result, err
}

func startedDuration(started time.Time) int64 {
	return time.Since(started).Milliseconds()
}

func errorText(err error) any {
	if err == nil {
		return nil
	}
	return err.Error()
}
