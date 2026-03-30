# Claude Desktop Test Prompt — Candor Foods Production Planner

Copy everything below the line and paste it into Claude Desktop chat.

---

You are a production planner for Candor Foods. You have access to the "Candor Foods Production Planner" MCP tools. I need you to help me create a daily production plan for entity **cfpl** (Candor Foods Pvt. Ltd.).

Please follow these steps in order:

## Step 1: Sync Fulfillment
First, sync all pending FG Sales Order lines to the fulfillment table for entity `cfpl` using the `sync_fulfillment` tool. Tell me how many records were synced.

## Step 2: Check Demand
Use `get_demand_summary` to show me the aggregated pending demand for entity `cfpl`. Summarize:
- Which products have the highest pending quantity?
- Which customers have the earliest deadlines?
- Total number of product-customer combinations waiting.

## Step 3: Get Fulfillment Records
Use `get_fulfillment_list` to get open and partial fulfillment records for entity `cfpl` (page 1, 50 per page). From the results, identify the **top 10 most urgent fulfillment IDs** based on earliest deadline and highest priority (lowest priority number = most urgent).

## Step 4: Get Planning Context
Using those top 10 fulfillment IDs from Step 3, call `get_planning_context` with entity `cfpl` and today's date. Analyze the returned context and summarize:
- How many demand items have BOMs vs no BOMs?
- What machines are available and their capacity?
- Current inventory levels (RM, PM, FG stores)
- Any in-progress jobs or pending indents?

## Step 5: Generate Production Plan
Based on the planning context from Step 4, create a daily production schedule following these rules:
1. Prioritize by delivery deadline (earliest first), then priority number (1 = highest)
2. For PRODUCTION type: schedule full process route (sorting → roasting → packaging etc.)
3. For REPACKAGING type: packaging-only, source FG from fg_store
4. Respect machine capacity — max 8 hours per shift per machine
5. Flag material shortages
6. Group same products on same machine to minimize changeover

Then call `save_production_plan` with:
- entity: `cfpl`
- plan_type: `daily`
- date_from: today's date
- date_to: today's date
- schedule_json: your generated schedule array
- material_check_json: your material availability checks
- risk_flags_json: any warnings or issues

## Step 6: Verify the Plan
Use `list_plans` filtered by entity `cfpl` and status `draft` to confirm the plan was saved. Then use `get_plan_detail` with the plan_id from Step 5 to show me the full plan details.

## Final Summary
After all steps, give me a clear summary with:
- Total demand planned (kg)
- Number of schedule lines
- Machines utilized
- Material shortages (if any)
- Risk flags (if any)
- Recommendations for tomorrow
