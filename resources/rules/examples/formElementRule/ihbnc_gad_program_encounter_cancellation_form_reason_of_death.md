---
rule_kind: formElementRule
intent: show this field only when the cancellation form recorded a specific reason
  (e.g. show death details when cancellation reason is Child Death)
entity_param: programEncounter
encounter_types: []
concepts: []
source_org: IHBNC GAD
field_name: Reason of death
kind: visibility
---
```js
"use strict";
({params, imports}) => {
    const programEncounter = params.entity;
    const formElement = params.formElement;
    
const cancelReasonObs = programEncounter.findCancelEncounterObservation('Visit cancel reason');
const answer = _.isNil(cancelReasonObs) ? undefined : cancelReasonObs.getReadableValue();  

let isVisible = false;
if (answer == 'Child Death') 
  isVisible = true;
  
const status = new imports.rulesConfig.FormElementStatus(formElement.uuid, isVisible);     
 
  return status;
  };
```
