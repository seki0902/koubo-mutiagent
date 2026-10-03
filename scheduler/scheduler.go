package scheduler

import (
	"fmt"
	"strings"
	"sync"
	"time"

	"koubo-video-tool/chanjing"
	"koubo-video-tool/store"
)

const (
	maxPolls               = 30
	maxInterval            = 30 * time.Second
	maxEmptyFailureRetries = 3
)

type pendingJob struct {
	taskID            string
	token             string
	pollCount         int
	interval          time.Duration
	emptyFailureCount int
}

type Scheduler struct {
	client   chanjing.ClientInterface
	onUpdate func(store.Task)
	mu       sync.Mutex
	jobs     map[string]*pendingJob
	stopCh   chan struct{}
	once     sync.Once
}

func New(client chanjing.ClientInterface, onUpdate func(store.Task)) *Scheduler {
	return &Scheduler{
		client:   client,
		onUpdate: onUpdate,
		jobs:     make(map[string]*pendingJob),
		stopCh:   make(chan struct{}),
	}
}

func (s *Scheduler) Add(taskID string, token string) {
	s.mu.Lock()
	s.jobs[taskID] = &pendingJob{
		taskID:   taskID,
		token:    token,
		interval: 2 * time.Second,
	}
	s.mu.Unlock()
	go s.poll(taskID)
}

func (s *Scheduler) Restore(tasks []store.Task) {
	for _, t := range tasks {
		if t.Status == "pending" || t.Status == "generating" {
			s.mu.Lock()
			s.jobs[t.TaskID] = &pendingJob{
				taskID:    t.TaskID,
				token:     "",
				pollCount: t.PollCount,
				interval:  nextInterval(t.PollCount),
			}
			s.mu.Unlock()
			go s.poll(t.TaskID)
		}
	}
}

func (s *Scheduler) Refresh(taskID string) (store.Task, error) {
	if s == nil || s.client == nil {
		return store.Task{}, fmt.Errorf("scheduler unavailable")
	}

	token, err := s.client.GetToken()
	if err != nil {
		return store.Task{}, err
	}

	status, err := s.client.GetVideoStatus(token, taskID)
	if err != nil && isAccessTokenExpired(err) {
		s.client.InvalidateTokenCache()
		token, err = s.client.GetToken()
		if err == nil {
			status, err = s.client.GetVideoStatus(token, taskID)
		}
	}
	if err != nil {
		return store.Task{}, err
	}

	task := store.Task{
		TaskID:   taskID,
		Progress: status.Progress,
		VideoURL: status.VideoURL,
		Error:    status.Msg,
	}

	switch {
	case status.Status == 30 || status.QueueStatus == "completed":
		s.mu.Lock()
		delete(s.jobs, taskID)
		s.mu.Unlock()
		task.Status = "done"
		task.Progress = 100
		task.Error = ""
		task.CompletedAt = time.Now().UTC().Format(time.RFC3339)
	case status.Status >= 40 || status.QueueStatus == "failed":
		task.Error = strings.TrimSpace(task.Error)
		if task.Error == "" {
			task.Error = strings.TrimSpace(status.QueueDesc)
		}
		if task.Error == "" {
			// The provider can briefly report a 4X/5X status at 100% and then
			// settle on completed. Keep polling instead of persisting a false
			// terminal failure when it gives us no actual failure reason.
			task.Status = "generating"
			task.VideoURL = ""
			s.Add(taskID, token)
		} else {
			s.mu.Lock()
			delete(s.jobs, taskID)
			s.mu.Unlock()
			task.Status = "failed"
			task.VideoURL = ""
			task.CompletedAt = time.Now().UTC().Format(time.RFC3339)
		}
	default:
		task.Status = "generating"
		task.VideoURL = ""
		task.Error = ""
	}

	if s.onUpdate != nil {
		s.onUpdate(task)
	}

	return task, nil
}

func (s *Scheduler) Stop() {
	s.once.Do(func() {
		close(s.stopCh)
		s.mu.Lock()
		defer s.mu.Unlock()
		s.jobs = make(map[string]*pendingJob)
	})
}

func (s *Scheduler) poll(taskID string) {
	for {
		select {
		case <-s.stopCh:
			return
		default:
		}

		s.mu.Lock()
		job, ok := s.jobs[taskID]
		if !ok {
			s.mu.Unlock()
			return
		}

		if job.pollCount >= maxPolls {
			delete(s.jobs, taskID)
			s.mu.Unlock()
			s.update(taskID, "timeout", 0, "", fmt.Sprintf("polling timeout after %d attempts", maxPolls))
			return
		}

		token := job.token
		if token == "" {
			var err error
			token, err = s.client.GetToken()
			if err != nil {
				delete(s.jobs, taskID)
				s.mu.Unlock()
				s.update(taskID, "failed", 0, "", fmt.Sprintf("token refresh failed: %v", err))
				return
			}
			job.token = token
		}

		interval := job.interval
		s.mu.Unlock()

		status, err := s.client.GetVideoStatus(token, taskID)
		if err != nil && isAccessTokenExpired(err) {
			s.client.InvalidateTokenCache()
			s.mu.Lock()
			if job, ok := s.jobs[taskID]; ok {
				job.token = ""
			}
			s.mu.Unlock()

			token, err = s.client.GetToken()
			if err == nil {
				status, err = s.client.GetVideoStatus(token, taskID)
			}
		}
		if err != nil {
			s.mu.Lock()
			delete(s.jobs, taskID)
			s.mu.Unlock()
			s.update(taskID, "failed", 0, "", fmt.Sprintf("poll error: %v", err))
			return
		}

		switch {
		case status.Status == 30 || status.QueueStatus == "completed":
			s.mu.Lock()
			delete(s.jobs, taskID)
			s.mu.Unlock()
			s.update(taskID, "done", 100, status.VideoURL, "")
			return
		case status.Status >= 40 || status.QueueStatus == "failed":
			errMsg := strings.TrimSpace(status.Msg)
			if errMsg == "" {
				errMsg = strings.TrimSpace(status.QueueDesc)
			}
			if errMsg == "" {
				s.mu.Lock()
				job, ok := s.jobs[taskID]
				if !ok {
					s.mu.Unlock()
					return
				}
				job.emptyFailureCount++
				if job.emptyFailureCount <= maxEmptyFailureRetries {
					job.pollCount++
					job.interval = nextInterval(job.pollCount)
					s.mu.Unlock()
					s.update(taskID, "generating", status.Progress, "", "")
					time.Sleep(interval)
					continue
				}
				delete(s.jobs, taskID)
				s.mu.Unlock()
				errMsg = fmt.Sprintf("platform reported failure status %d without a reason", status.Status)
			} else {
				s.mu.Lock()
				delete(s.jobs, taskID)
				s.mu.Unlock()
			}
			s.update(taskID, "failed", status.Progress, "", errMsg)
			return
		default:
			s.mu.Lock()
			job.pollCount++
			job.interval = nextInterval(job.pollCount)
			job.emptyFailureCount = 0
			s.mu.Unlock()
			s.update(taskID, "generating", status.Progress, "", "")
		}

		time.Sleep(interval)
	}
}

func isAccessTokenExpired(err error) bool {
	if err == nil {
		return false
	}
	msg := err.Error()
	return strings.Contains(msg, "AccessToken已失效") || strings.Contains(msg, "AccessTokenå·²å¤±æ") || strings.Contains(msg, "10400")
}

func nextInterval(pollCount int) time.Duration {
	d := time.Duration(1<<uint(pollCount)) * time.Second
	if d > maxInterval {
		d = maxInterval
	}
	if d < 2*time.Second {
		d = 2 * time.Second
	}
	return d
}

func (s *Scheduler) update(taskID, status string, progress int, videoURL, errMsg string) {
	task := store.Task{
		TaskID:   taskID,
		Status:   status,
		Progress: progress,
		VideoURL: videoURL,
		Error:    errMsg,
	}
	if status == "done" || status == "failed" || status == "timeout" {
		task.CompletedAt = time.Now().UTC().Format(time.RFC3339)
	}
	if s.onUpdate != nil {
		s.onUpdate(task)
	}
}
