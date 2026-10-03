package topicsearch

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"time"
)

// SourceFetchError is a capability-level classification for source fetching.
// The handler maps Kind to the Python/Go internal error contract.
type SourceFetchError struct {
	Kind       string
	StatusCode int
	Err        error
}

func (e *SourceFetchError) Error() string {
	if e.Err != nil {
		return e.Err.Error()
	}
	return e.Kind
}

func (e *SourceFetchError) Unwrap() error { return e.Err }

// SourceFetchResult is the bounded, cleaned page representation exposed to
// the Research Tool.  PublishedAt is left empty when the page does not expose
// a trustworthy publication date.
type SourceFetchResult struct {
	SourceURL   string `json:"source_url"`
	FinalURL    string `json:"final_url"`
	Title       string `json:"title"`
	Content     string `json:"content"`
	PublishedAt string `json:"published_at"`
	FetchedAt   string `json:"fetched_at"`
}

var sourceTitleRE = regexp.MustCompile(`(?is)<title\b[^>]*>(.*?)</title>`)

// FetchSource exposes the existing bounded HTML-cleaning capability without
// involving search ranking or any Research Agent decisions.
func (s *Service) FetchSource(ctx context.Context, rawURL string, maxChars int) (SourceFetchResult, error) {
	rawURL = strings.TrimSpace(rawURL)
	if rawURL == "" {
		return SourceFetchResult{}, &SourceFetchError{Kind: "invalid_request", Err: errors.New("source URL is blank")}
	}
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Host == "" || (parsed.Scheme != "http" && parsed.Scheme != "https") {
		return SourceFetchResult{}, &SourceFetchError{Kind: "invalid_request", Err: errors.New("source URL must be an HTTP(S) URL")}
	}
	if maxChars == 0 {
		maxChars = 6000
	}
	if maxChars < 1 || maxChars > 12000 {
		return SourceFetchResult{}, &SourceFetchError{Kind: "invalid_request", Err: errors.New("max_chars must be between 1 and 12000")}
	}

	client := s.HTTPClient
	if client == nil {
		client = newHTTPClient()
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return SourceFetchResult{}, &SourceFetchError{Kind: "invalid_request", Err: err}
	}
	req.Header.Set("User-Agent", "Koubo-Video-Tool/1.0 research-source-fetch")
	req.Header.Set("Accept", "text/html,application/xhtml+xml")
	req.Header.Set("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.7")
	req.Header.Set("Accept-Encoding", "identity")

	resp, err := client.Do(req)
	if err != nil {
		kind := "unavailable"
		if errors.Is(ctx.Err(), context.DeadlineExceeded) {
			kind = "timeout"
		} else {
			var netErr net.Error
			if errors.As(err, &netErr) && netErr.Timeout() {
				kind = "timeout"
			}
		}
		return SourceFetchResult{}, &SourceFetchError{Kind: kind, Err: err}
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusNotFound {
		return SourceFetchResult{}, &SourceFetchError{Kind: "not_found", StatusCode: resp.StatusCode}
	}
	if resp.StatusCode == http.StatusForbidden || resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusTooManyRequests {
		return SourceFetchResult{}, &SourceFetchError{Kind: "blocked", StatusCode: resp.StatusCode}
	}
	if resp.StatusCode < http.StatusOK || resp.StatusCode >= http.StatusMultipleChoices {
		return SourceFetchResult{}, &SourceFetchError{
			Kind:       "upstream_error",
			StatusCode: resp.StatusCode,
			Err:        fmt.Errorf("source fetch returned status %d", resp.StatusCode),
		}
	}

	body, err := io.ReadAll(io.LimitReader(resp.Body, 160<<10))
	if err != nil {
		return SourceFetchResult{}, &SourceFetchError{Kind: "unavailable", Err: err}
	}
	content := cleanHTML(string(body), maxChars)
	if strings.TrimSpace(content) == "" {
		return SourceFetchResult{}, &SourceFetchError{Kind: "blocked", Err: errors.New("source page has no readable content")}
	}

	finalURL := rawURL
	if resp.Request != nil && resp.Request.URL != nil {
		finalURL = resp.Request.URL.String()
	}
	title := parsed.Host
	if match := sourceTitleRE.FindStringSubmatch(string(body)); len(match) > 1 {
		if cleaned := strings.TrimSpace(cleanHTML(match[1], 300)); cleaned != "" {
			title = cleaned
		}
	}
	return SourceFetchResult{
		SourceURL:   rawURL,
		FinalURL:    finalURL,
		Title:       title,
		Content:     content,
		PublishedAt: "",
		FetchedAt:   time.Now().UTC().Format(time.RFC3339),
	}, nil
}
