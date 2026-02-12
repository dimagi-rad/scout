# Scout Todo Items

## Plan (in implementation order)

1. [x] Change recipe 'edit' button to 'view' in list
2. [x] Convert recipe steps -> single markdown field
3. [x] Allow editing recipes (edit button on the 'view' page) - done as part of #2, RecipeDetail has editable prompt + Save
4. [x] Remove public sharing of recipes (only have public sharing of results)
5. [x] When running a recipe, redirect to result view (not dialog)
6. [x] Artifacts should be visible in recipe results
7. [x] Clicking a chat in the sidebar should navigate to /chat
8. [x] Tool calls & thinking should be expandable to show details
9. [x] Check that 'thinking' steps are being displayed in the chat - added reasoning block support to stream.py
10. [ ] (human) Review data loading features
11. [ ] CSV import from data_buddy_import.py - needs human input on UX design
    - CLI tool exists at data_buddy_import.py with pandas+sqlalchemy
    - Need to decide: file upload UI? Which DB/schema? Table naming? Column type overrides?
12. [ ] Allow sharing of chat history (public / team) including artifacts
    - Thread model (apps/chat/models.py) needs is_shared/is_public/share_token fields
    - Chat messages are in LangGraph PostgreSQL checkpointer, not Django models
    - Would need to read from checkpointer for shared view - substantial feature
13. [ ] (human) Can we make recipe results into a continuable chat session?

## Notes
- Items tagged (human) are skipped until human review
- Items 11-13 are larger features requiring design decisions
