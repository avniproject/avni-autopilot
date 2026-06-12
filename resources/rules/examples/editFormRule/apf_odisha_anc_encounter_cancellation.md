---
rule_kind: editFormRule
intent: block editing a cancelled encounter once its scheduled month has passed
entity_param: individual
encounter_types: []
concepts: []
source_org: APF Odisha
---
```js
"use strict";
 ({ params, imports }) => {
  const { entity } = params; // entity = encounter
  const moment = imports.moment;
 

  const isCancelledPreviousMonth =
  entity.cancelDateTime &&
  moment(entity.earliestVisitDateTime).isBefore(moment(), 'month');
 

  if (isCancelledPreviousMonth) {
  return {
  eligible: {
  value: false,
  message: "Cancelled visits from a previous month cannot be edited."
  }
  };
  }
 

 

  return {
  eligible: {
  value: true
  }
  };
 };
```
