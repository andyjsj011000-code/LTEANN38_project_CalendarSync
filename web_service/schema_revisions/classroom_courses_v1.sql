CREATE TABLE IF NOT EXISTS web_classroom_hidden_courses (
    user_id TEXT NOT NULL REFERENCES web_users(id) ON DELETE CASCADE,
    course_key TEXT NOT NULL,
    PRIMARY KEY (user_id, course_key)
);
