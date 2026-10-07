// Airtable automation -> "Run script" action, for the SUPPLIER ORDER tables.
// Trigger: "When record matches conditions" on the checkbox (one automation per checkbox, 4 total):
//   orders GRAF  : «Create PDF with clients»  -> templateName "Заказ GRAF с клиентами"
//                  «Create PDF with NO clients» -> "Заказ GRAF без клиентов"
//   orders CHINA : «Create PDF order with clients CHINA»      -> "Заказ Китай с клиентами"
//                  «Create PDF order with NO clients CHINA»   -> "Заказ Китай без клиентов"
// Input variables: recordId (the triggering record), tableId, templateName (constants per automation).
// Same webhook as the Inquiries automation ("Automation 1") -- copy its URL here.
const WEBHOOK_URL = "https://rosmadocgen-production.up.railway.app/generate-document";

const { recordId, tableId, templateName } = input.config();

const resp = await fetch(WEBHOOK_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ record_id: recordId, table_id: tableId, template_name: templateName }),
});
if (resp.status !== 202) {
    throw new Error(`docgen returned ${resp.status}: ${await resp.text()}`);
}
output.set("accepted", true);
