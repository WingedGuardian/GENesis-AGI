- **A message can no longer plant a procedure Genesis will follow later.** One
  older path that turns a finished interaction into a reusable, stored procedure
  was handing the request and the reply to the model as plain text, with nothing
  marking where they ended. Text written by whoever sent the message could
  therefore pose as the pipeline's own instructions — and because the result of
  that step is SAVED and recalled by later sessions, a single crafted message
  could leave behind a procedure Genesis would go on following. Both pieces of
  text now sit inside markers the text itself cannot forge, the same protection
  the review prompts already use.
