---
rule_kind: decisionRule
intent: set the subject's initial life status (Alive) on registration
entity_param: individual
encounter_types: []
concepts: []
source_org: Sakhi App
---
```js
"use strict";
 ({params, imports}) => {
  const individual = params.entity;
  const decisions = params.decisions;
  
  decisions.registrationDecisions.push({name : "Individuals status", value : "Alive"});
  
  return decisions;
 };
```
